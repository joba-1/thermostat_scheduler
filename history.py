"""Per-room history charts for the status web page.

Pulls the last few hours of room temperature, heat-pump activity, window-open and
window state from Home Assistant's InfluxDB (job4, db `homeassistant`) and renders a
dependency-free inline SVG. All InfluxDB access goes through `InfluxClient`, whose
`_fetch` can be replaced in tests so nothing here ever needs a live database.

Entity ids are derived from the friendly names already in the thermostat config by
the same slug rule Home Assistant uses (lowercase, umlauts transliterated). Anything
that doesn't slugify cleanly can be overridden per room in `web.history.entities`.
"""
import html
import json
import time
import unicodedata
import urllib.parse
import urllib.request

# Heat-pump entities are global (one heat pump), not per room.
# compressor_activity is a STRING state ('cooling'/'heating'/'hot water'/'off') — the
# authoritative signal. The thermostat's cooling_on flag is unreliable (it reads off
# during active cooling), so it is not used.
HP_ACTIVITY = 'ems_esp_boiler_compressor_activity'
HP_OUTDOOR_TEMP = 'ems_esp_thermostat_damped_outdoor_temperature'

# Allowed chart windows (hours). Whitelisted so no caller-supplied value reaches a query.
HOURS_CHOICES = (6, 24, 72)
DEFAULT_HOURS = 24

_TRANSLIT = {'ä': 'a', 'ö': 'o', 'ü': 'u', 'ß': 'ss',
             'á': 'a', 'à': 'a', 'é': 'e', 'è': 'e', 'í': 'i', 'ó': 'o', 'ú': 'u'}


def slugify(name):
    """Home Assistant entity-id slug for a friendly name.

    Lowercase, transliterate German umlauts / common accents to ASCII, then map any
    run of non-alphanumeric characters to a single underscore. Verified against the
    live db: 'Waschküche' -> 'waschkuche', 'Arbeitszimmer Fenster' -> 'arbeitszimmer_fenster'.
    """
    s = (name or '').strip().lower()
    s = ''.join(_TRANSLIT.get(ch, ch) for ch in s)
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode('ascii')
    out = []
    prev_us = False
    for ch in s:
        if ch.isalnum():
            out.append(ch)
            prev_us = False
        elif not prev_us:
            out.append('_')
            prev_us = True
    return ''.join(out).strip('_')


def room_entities(room, item, overrides=None):
    """Derive the InfluxDB entity ids for one room from its existing config wiring.

    `item` is the `thermostats[room]` config dict. `overrides` is an optional
    `{field: entity_id}` map (from `web.history.entities[room]`) that wins per field.
    """
    sensors = (item or {}).get('sensors', {}) or {}
    temp = sensors.get('temperature')
    windows = sensors.get('windows') or []
    # Prefer the configured room sensor; otherwise fall back to the TRV's own
    # local_temperature (the app's comfort fallback for rooms without a sensor).
    temp_entity = (slugify(temp) + '_temperature' if temp
                   else slugify(f"{room} Thermostat") + '_local_temperature')
    ent = {
        'temperature': temp_entity,
        'windows': [slugify(w) + '_contact' for w in windows],
        'activity': HP_ACTIVITY,
        'outdoor': HP_OUTDOOR_TEMP,
    }
    for k, v in (overrides or {}).items():
        ent[k] = v
    return ent


# --- interval algebra (pure, testable) -------------------------------------------

def fold_intervals(rows, t_start, t_end, truthy=lambda v: bool(v)):
    """Fold a step-interpreted state series into [(start, end), ...] 'on' intervals.

    `rows` is `[(epoch, value), ...]` ascending; each sample holds until the next one
    (the last until `t_end`). A leading sample at/just after `t_start` whose value is
    on therefore covers from `t_start`. Pass a synthetic first row at `t_start` (the
    last value *before* the range) to seed the state already-on at the window edge.
    """
    intervals = []
    open_at = None
    for ts, val in rows:
        ts = max(ts, t_start)
        if truthy(val):
            if open_at is None:
                open_at = ts
        else:
            if open_at is not None:
                if ts > open_at:
                    intervals.append((open_at, ts))
                open_at = None
    if open_at is not None and t_end > open_at:
        intervals.append((open_at, t_end))
    return intervals


def union_intervals(*lists):
    """Merge several interval lists into one non-overlapping, sorted list."""
    spans = sorted(s for lst in lists for s in lst)
    merged = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def subtract_intervals(base, cut):
    """base minus cut: the parts of `base` not covered by any span in `cut`."""
    cut = union_intervals(cut)
    out = []
    for a, b in base:
        cur = a
        for ca, cb in cut:
            if cb <= cur or ca >= b:
                continue
            if ca > cur:
                out.append((cur, min(ca, b)))
            cur = max(cur, cb)
            if cur >= b:
                break
        if cur < b:
            out.append((cur, b))
    return [s for s in out if s[1] > s[0]]


class InfluxClient:
    """Minimal read-only InfluxDB 1.x HTTP client (no external dependency)."""

    def __init__(self, url='http://job4:8086', database='homeassistant', timeout=4):
        self.url = url.rstrip('/')
        self.database = database
        self.timeout = timeout

    def _fetch(self, q):
        """Run one InfluxQL query, return series value rows ([[epoch, value], ...]).

        Returns [] for a missing series, an empty result, or any network/parse error
        so a slow or down database degrades one chart layer, never the whole page.
        """
        params = urllib.parse.urlencode({'db': self.database, 'q': q, 'epoch': 's'})
        try:
            with urllib.request.urlopen(f"{self.url}/query?{params}",
                                        timeout=self.timeout) as r:
                doc = json.load(r)
            series = doc['results'][0].get('series')
            return series[0]['values'] if series else []
        except Exception:
            return []

    @staticmethod
    def _esc(entity):
        # entity ids are derived from config / a whitelist, but quote defensively
        return str(entity).replace("'", "")

    def series(self, entity, hours, agg='mean', step=None):
        """A down-sampled numeric series for `entity` over the last `hours`."""
        if not entity:
            return []
        step = step or self._step(hours)
        q = (f'SELECT {agg}(value) FROM /.*/ '
             f"WHERE entity_id='{self._esc(entity)}' AND time>now()-{int(hours)}h "
             f'GROUP BY time({step}) fill(none)')
        return [(int(t), v) for t, v in self._fetch(q) if v is not None]

    def state_intervals(self, entity, hours, t_start, t_end, truthy=lambda v: bool(v)):
        """'On' intervals for a 0/1 (or numeric) state entity, seeded with the value
        in effect at the start of the window so an already-open window still shows."""
        if not entity:
            return []
        e = self._esc(entity)
        rows = [(int(t), v) for t, v in self._fetch(
            f"SELECT value FROM /.*/ WHERE entity_id='{e}' "
            f'AND time>now()-{int(hours)}h')]
        seed = self._fetch(
            f"SELECT value FROM /.*/ WHERE entity_id='{e}' "
            f'AND time<now()-{int(hours)}h ORDER BY time DESC LIMIT 1')
        if seed:
            rows = [(t_start, seed[0][1])] + rows
        rows.sort()
        return fold_intervals(rows, t_start, t_end, truthy)

    def string_state_intervals(self, entity, hours, t_start, t_end, match):
        """'On' intervals where a STRING state entity equals one of `match`.

        Used for the heat pump's compressor_activity ('cooling'/'heating'/...). Seeded
        with the value in effect at the window start so a long-running state still shows.
        """
        if not entity:
            return []
        e = self._esc(entity)
        rows = [(int(t), s) for t, s in self._fetch(
            f'SELECT state FROM "state" WHERE entity_id=\'{e}\' '
            f'AND time>now()-{int(hours)}h')]
        seed = self._fetch(
            f'SELECT state FROM "state" WHERE entity_id=\'{e}\' '
            f'AND time<now()-{int(hours)}h ORDER BY time DESC LIMIT 1')
        if seed:
            rows = [(t_start, seed[0][1])] + rows
        rows.sort(key=lambda r: r[0])
        return fold_intervals(rows, t_start, t_end, truthy=lambda s: s in match)

    @staticmethod
    def _step(hours):
        return '5m' if hours <= 6 else '15m' if hours <= 24 else '1h'


def collect_room_history(client, ent, hours, now=None):
    """Gather everything one room chart needs. Returns a dict of series + interval bands."""
    now = int(now if now is not None else time.time())
    t_start = now - int(hours) * 3600

    temp = client.series(ent.get('temperature'), hours)
    outdoor = client.series(ent.get('outdoor'), hours)

    # Cooling vs heating from the authoritative compressor_activity string. 'hot water'
    # (DHW) and 'off' are not room conditioning, so they're excluded from hp_active.
    act = ent.get('activity')
    hp_cooling = client.string_state_intervals(act, hours, t_start, now, {'cooling'})
    hp_heating = client.string_state_intervals(act, hours, t_start, now,
                                               {'heating', 'warm water heating'})
    hp_active = union_intervals(hp_cooling, hp_heating)

    window = union_intervals(*[
        client.state_intervals(w, hours, t_start, now) for w in ent.get('windows', [])
    ]) if ent.get('windows') else []

    # "Actually conditioning": heat pump producing AND the room's window closed (the
    # valves are forced open in cooling). We deliberately do NOT gate on the TRV's
    # valve_opening_degree — TRVZBs report it only on change (often once in many
    # hours), so before the first sample the band would be wrongly blank.
    conditioned = subtract_intervals(hp_active, window)

    return {
        'now': now, 't_start': t_start, 'hours': int(hours),
        'temp': temp, 'outdoor': outdoor,
        'hp_cooling': hp_cooling, 'hp_heating': hp_heating,
        'window': window, 'conditioned': conditioned,
    }


def _intersect(a, b):
    """Intersection of two interval lists."""
    a, b = union_intervals(a), union_intervals(b)
    out, i, j = [], 0, 0
    while i < len(a) and j < len(b):
        lo, hi = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if hi > lo:
            out.append((lo, hi))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


# --- SVG rendering ---------------------------------------------------------------
# Fonts are sized generously in the viewBox space because the SVG scales down to the
# phone-portrait width — small px values become unreadable there.

_W, _H = 720, 300          # plot box (narrower viewBox -> larger apparent text on phones)
_L, _R, _T, _B = 56, 14, 14, 40   # margins inside the plot box
_STRIPE_H = 24
_STRIPE_GAP = 7
_FS = 16                   # axis + stripe-label font size
_FS_LEG = 16               # legend font size
_FS_EMPTY = 22

_COL = {
    'temp': '#b42318', 'outdoor': '#6b7280',
    'cooling': '#1f6feb', 'heating': '#bf6b00',
    'window': '#d4a017', 'conditioned': '#1a7f37', 'grid': '#e5e7eb',
    'label': '#6b7280',
}


def _x(t, t0, t1, x0, x1):
    return x0 + (t - t0) / max(t1 - t0, 1) * (x1 - x0)


def _polyline(pts, t0, t1, vmin, vmax, x0, x1, y0, y1, color, dash=None):
    if not pts:
        return ''
    span = max(vmax - vmin, 0.1)
    coords = ' '.join(
        f"{_x(t, t0, t1, x0, x1):.1f},{y1 - (v - vmin) / span * (y1 - y0):.1f}"
        for t, v in pts)
    d = f' stroke-dasharray="{dash}"' if dash else ''
    return (f'<polyline fill="none" stroke="{color}" stroke-width="2.2" '
            f'stroke-linejoin="round" points="{coords}"{d}/>')


def _stripe(segments, t0, t1, x0, x1, y, label):
    """One stripe row. `segments` is [(intervals, color), ...] so a single row can
    carry more than one colour (the unified heat-pump row: cooling + heating)."""
    rects = ''.join(
        f'<rect x="{_x(a, t0, t1, x0, x1):.1f}" y="{y}" '
        f'width="{max(_x(b, t0, t1, x0, x1) - _x(a, t0, t1, x0, x1), 0.6):.1f}" '
        f'height="{_STRIPE_H}" fill="{color}"/>'
        for intervals, color in segments for a, b in intervals)
    return (f'<text x="{x0 - 7}" y="{y + _STRIPE_H * 0.5 + _FS * 0.35:.1f}" '
            f'text-anchor="end" font-size="{_FS}" fill="{_COL["label"]}">'
            f'{html.escape(label)}</text>'
            f'<rect x="{x0}" y="{y}" width="{x1 - x0}" height="{_STRIPE_H}" '
            f'fill="none" stroke="{_COL["grid"]}"/>{rects}')


def _legend(items, x0, x1, y, font):
    """Flow legend items left-to-right, wrapping to a new line at x1. Each item is
    (kind, color, dash, text) with kind 'line' (temperature trace) or 'box' (stripe)."""
    sw, gap, pad = 24, 7, 26
    out, cx, line_h = [], x0, font + 13
    for kind, color, dash, text in items:
        item_w = sw + gap + len(text) * font * 0.6 + pad
        if cx + item_w > x1 and cx > x0:
            cx, y = x0, y + line_h
        if kind == 'line':
            d = f' stroke-dasharray="{dash}"' if dash else ''
            out.append(f'<line x1="{cx}" y1="{y}" x2="{cx + sw}" y2="{y}" '
                       f'stroke="{color}" stroke-width="2.6"{d}/>')
        else:
            out.append(f'<rect x="{cx}" y="{y - font * 0.55:.1f}" width="{sw}" '
                       f'height="{font * 0.8:.1f}" rx="2" fill="{color}"/>')
        out.append(f'<text x="{cx + sw + gap}" y="{y + font * 0.35:.1f}" '
                   f'font-size="{font}" fill="{_COL["label"]}">{html.escape(text)}</text>')
        cx += item_w
    return ''.join(out), y + line_h


def render_room_svg(room, data):
    """Render one room's history as a standalone inline SVG string."""
    t0, t1 = data['t_start'], data['now']
    x0, x1 = _L, _W - _R
    y0, y1 = _T, _H - _B
    temp, outdoor = data['temp'], data['outdoor']

    vals = [v for _, v in temp] + [v for _, v in outdoor]
    body = []
    if not vals:
        body.append(f'<text x="{_W/2}" y="{_H/2}" text-anchor="middle" '
                    f'fill="{_COL["label"]}" font-size="{_FS_EMPTY}">no temperature '
                    f'history for the selected window</text>')
    else:
        vmin, vmax = min(vals), max(vals)
        pad = max((vmax - vmin) * 0.1, 0.5)
        vmin, vmax = vmin - pad, vmax + pad
        # y gridlines + °C ticks
        for frac in (0, 0.25, 0.5, 0.75, 1):
            yy = y1 - frac * (y1 - y0)
            tv = vmin + frac * (vmax - vmin)
            body.append(f'<line x1="{x0}" y1="{yy:.1f}" x2="{x1}" y2="{yy:.1f}" '
                        f'stroke="{_COL["grid"]}"/>')
            body.append(f'<text x="{x0 - 7}" y="{yy + _FS * 0.35:.1f}" '
                        f'text-anchor="end" font-size="{_FS}" fill="{_COL["label"]}">'
                        f'{tv:.1f}°</text>')
        # x time ticks (~6 labels)
        n = 6
        for i in range(n + 1):
            t = t0 + (t1 - t0) * i / n
            xx = _x(t, t0, t1, x0, x1)
            lbl = time.strftime('%H:%M', time.localtime(t))
            body.append(f'<text x="{xx:.1f}" y="{y1 + _FS + 5:.0f}" '
                        f'text-anchor="middle" font-size="{_FS}" '
                        f'fill="{_COL["label"]}">{lbl}</text>')
        body.append(_polyline(outdoor, t0, t1, vmin, vmax, x0, x1, y0, y1,
                              _COL['outdoor'], dash='5 4'))
        body.append(_polyline(temp, t0, t1, vmin, vmax, x0, x1, y0, y1, _COL['temp']))

    # stripe rows beneath the plot: conditioned (green) first, the unified heat/cool
    # row next, window open last. Labels are short (legend below spells them out).
    y = _H + 6
    rows = [('Cond', [(union_intervals(data['conditioned']), _COL['conditioned'])]),
            ('HP', [(union_intervals(data['hp_cooling']), _COL['cooling']),
                    (union_intervals(data['hp_heating']), _COL['heating'])]),
            ('Open', [(union_intervals(data['window']), _COL['window'])])]
    for label, segments in rows:
        body.append(_stripe(segments, t0, t1, x0, x1, y, label))
        y += _STRIPE_H + _STRIPE_GAP

    legend_items = [
        ('line', _COL['temp'], None, 'room temp'),
        ('line', _COL['outdoor'], '5 4', 'outdoor (HP ref)'),
        ('box', _COL['conditioned'], None, 'Cond = conditioned'),
        ('box', _COL['cooling'], None, 'HP cooling'),
        ('box', _COL['heating'], None, 'HP heating'),
        ('box', _COL['window'], None, 'Open = window open'),
    ]
    legend_svg, y = _legend(legend_items, x0, x1, y + _FS_LEG, _FS_LEG)
    body.append(legend_svg)

    total_h = int(y + 6)
    return (f'<svg viewBox="0 0 {_W} {total_h}" width="100%" '
            f'font-family="system-ui,sans-serif" role="img" '
            f'aria-label="{html.escape(room)} history">{"".join(body)}</svg>')
