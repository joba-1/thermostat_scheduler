"""Per-room history charts for the status web page.

Pulls the last few hours of room temperature, heat-pump activity and window-open
state from Home Assistant's InfluxDB (job4, db `homeassistant`) and renders a
dependency-free inline SVG. All InfluxDB access goes through `InfluxClient`, whose
`_fetch` can be replaced in tests so nothing here ever needs a live database.

A device's HA entity id is resolved from its zigbee ieee (stable) + friendly name
(see devices.ha_entity_candidates): the caller passes an ordered list of candidate
entity ids per signal and the query uses whichever returns data — so a sensor that
HA logged under its raw ieee (no friendly name) still resolves. The chart `spec`
(candidate lists) is built by the Manager from its registry-resolved device maps.
"""
import html
import json
import math
import time
import urllib.parse
import urllib.request

# Heat-pump entities are global (one heat pump), not per room.
# compressor_activity is a STRING state ('cooling'/'heating'/'hot water'/'off') — the
# authoritative signal. The thermostat's cooling_on flag is unreliable (it reads off
# during active cooling), so it is not used.
HP_ACTIVITY = 'ems_esp_boiler_compressor_activity'
HP_OUTDOOR_TEMP = 'ems_esp_thermostat_damped_outdoor_temperature'   # damped (HP ref)
HP_OUTDOOR_RAW = 'ems_esp_boiler_outside_temperature'               # real measured

# Allowed chart windows (hours). Whitelisted so no caller-supplied value reaches a query.
HOURS_CHOICES = (6, 24, 72, 168, 720)
DEFAULT_HOURS = 24
_HOURS_LABELS = {6: '6h', 24: '24h', 72: '3d', 168: '1w', 720: '1mo'}


def hours_label(h):
    return _HOURS_LABELS.get(h, f'{h}h')

# Cheap contact sensors (e.g. TS0203) routinely DROP their close event, leaving the
# logged state stuck "open" — for the *seed* (state at window start) and *in-window*
# (consecutive opens with no close fold into one giant span). Two guards, both 12 h
# (comfortably exceeds a real overnight airing, kills the multi-day artifacts):
#  - SEED_MAX_AGE: ignore a pre-window state-seed older than this.
#  - MAX_OPEN: hide a window-open span longer than this (treated as a dropped close).
SEED_MAX_AGE = 12 * 3600
MAX_OPEN = 12 * 3600

# A temperature that holds steady logs no new HA points (on-change logging), so the
# line would otherwise stop at the last change. Extend it flat to both chart edges
# across a gap of up to this long; a longer gap is left open so a genuinely dead /
# offline sensor still shows a break rather than a misleading flat line.
HOLD_MAX = 12 * 3600


def hold_to_edges(series, t_start, t_end, max_gap=HOLD_MAX):
    """Flat-extend a value series to the window edges (see HOLD_MAX). `series` is
    [(epoch, value), ...] ascending."""
    if not series:
        return series
    out = list(series)
    if t_start < out[0][0] <= t_start + max_gap:
        out.insert(0, (t_start, out[0][1]))
    if t_end - max_gap <= out[-1][0] < t_end:
        out.append((t_end, out[-1][1]))
    return out


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
        if seed and t_start - int(seed[0][0]) <= SEED_MAX_AGE:
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
        if seed and t_start - int(seed[0][0]) <= SEED_MAX_AGE:
            rows = [(t_start, seed[0][1])] + rows
        rows.sort(key=lambda r: r[0])
        return fold_intervals(rows, t_start, t_end, truthy=lambda s: s in match)

    def series_merged(self, candidates, hours):
        """series() merged across all candidate entity ids, sorted by time.

        Candidates are the same physical device under different HA entity ids (named
        slug + 0x<ieee> fallback). A HA rename splits history across two ids at the
        rename instant, so the union — not first-with-data — keeps the whole window
        visible. Their samples don't overlap in time, so concatenation is correct.
        """
        rows = []
        for ent in candidates or []:
            rows += self.series(ent, hours)
        rows.sort(key=lambda r: r[0])
        return rows

    def series_best(self, groups, hours):
        """Pick the richest of several candidate *groups* and return its merged series.

        Each group is one physical device's entity ids (merged within via
        series_merged); different groups are *different* sources (e.g. the room air
        sensor vs the TRV's own local_temperature), so we choose the one with the most
        data rather than merging — a dead/frozen air sensor falls back to the live TRV.
        """
        best = []
        for group in groups or []:
            s = self.series_merged(group, hours)
            if len(s) > len(best):
                best = s
        return best

    def state_intervals_merged(self, candidates, hours, t_start, t_end,
                               truthy=lambda v: bool(v)):
        """state_intervals() merged across candidate entity ids (see series_merged):
        union their in-window samples and seed from the most recent pre-window value
        among them, so a state that spans a HA rename folds into one continuous band."""
        rows, seeds = [], []
        for ent in candidates or []:
            e = self._esc(ent)
            rows += [(int(t), v) for t, v in self._fetch(
                f"SELECT value FROM /.*/ WHERE entity_id='{e}' "
                f'AND time>now()-{int(hours)}h')]
            s = self._fetch(f"SELECT value FROM /.*/ WHERE entity_id='{e}' "
                            f'AND time<now()-{int(hours)}h ORDER BY time DESC LIMIT 1')
            if s:
                seeds.append((int(s[0][0]), s[0][1]))
        # most recent pre-window value wins, but only if it isn't a stale (dropped-edge)
        # reading — see SEED_MAX_AGE (lossy contact sensors get stuck "open").
        if seeds:
            seeds.sort()
            if t_start - seeds[-1][0] <= SEED_MAX_AGE:
                rows.append((t_start, seeds[-1][1]))
        rows.sort(key=lambda r: r[0])
        return fold_intervals(rows, t_start, t_end, truthy)

    @staticmethod
    def _step(hours):
        return ('5m' if hours <= 6 else '15m' if hours <= 24 else '1h'
                if hours <= 72 else '2h' if hours <= 168 else '6h')


def collect_room_history(client, spec, hours, now=None):
    """Gather everything one room chart needs from a `spec` of entity candidates.

    spec = {'temp': [cand,...], 'outdoor': id, 'activity': id,
            'windows': [[cand,...], ...]}  — each candidate list is tried in order
    (named slug first, then the device's 0x<ieee> form). Returns series + bands.
    """
    now = int(now if now is not None else time.time())
    t_start = now - int(hours) * 3600

    temp = hold_to_edges(client.series_best(spec.get('temp'), hours), t_start, now)
    outdoor = hold_to_edges(client.series(spec.get('outdoor'), hours), t_start, now)
    outdoor_ref = hold_to_edges(client.series(spec.get('outdoor_ref'), hours),
                                t_start, now)

    # Cooling vs heating from the authoritative compressor_activity string. 'hot water'
    # (DHW) and 'off' are not room conditioning, so they're excluded from hp_active.
    act = spec.get('activity')
    hp_cooling = client.string_state_intervals(act, hours, t_start, now, {'cooling'})
    hp_heating = client.string_state_intervals(act, hours, t_start, now,
                                               {'heating', 'warm water heating'})
    hp_active = union_intervals(hp_cooling, hp_heating)

    window = union_intervals(*[
        client.state_intervals_merged(cands, hours, t_start, now)
        for cands in spec.get('windows', [])
    ]) if spec.get('windows') else []
    # Drop implausibly long "open" spans (a dropped close left the state stuck open);
    # see MAX_OPEN. Keeps week/month views from showing a phantom continuous band.
    window = [s for s in window if s[1] - s[0] <= MAX_OPEN]

    # "Actually conditioning": heat pump producing AND the room's window closed (the
    # valves are forced open in cooling). We deliberately do NOT gate on the TRV's
    # valve_opening_degree — TRVZBs report it only on change (often once in many
    # hours), so before the first sample the band would be wrongly blank.
    conditioned = subtract_intervals(hp_active, window)

    return {
        'now': now, 't_start': t_start, 'hours': int(hours),
        'temp': temp, 'outdoor': outdoor, 'outdoor_ref': outdoor_ref,
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
    'temp': '#b42318', 'outdoor': '#6b7280', 'outdoor_ref': '#b8bcc4',
    'cooling': '#1f6feb', 'heating': '#bf6b00',
    'window': '#d4a017', 'conditioned': '#1a7f37', 'grid': '#e5e7eb',
    'label': '#6b7280',
}


def _x(t, t0, t1, x0, x1):
    return x0 + (t - t0) / max(t1 - t0, 1) * (x1 - x0)


def _nice_step(span):
    """Smallest 'nice' axis step (…0.5,1,2,5,10…) giving at most ~6 intervals."""
    span = max(span, 0.5)
    for s in (0.5, 1, 2, 5, 10, 20, 50, 100):
        if span / s <= 6:
            return s
    return 200


def _temp_axis(vlo, vhi):
    """Round [min,max] data temps out to nice gridline bounds; return (vmin, vmax,
    step) so axis labels land on round values (24°, 26°…) not 24.1°."""
    step = _nice_step(vhi - vlo)
    vmin = math.floor(vlo / step) * step
    vmax = math.ceil(vhi / step) * step
    while vmax - vmin < 1.5 * step:        # guarantee a few gridlines, even if flat
        vmin -= step
        vmax += step
    return vmin, vmax, step


def _time_ticks(t0, t1, hours):
    """Even local-time tick boundaries within [t0, t1] (round hours / midnights), so
    axis labels read 16:00 / Mon 00:00 rather than the window's arbitrary edge time."""
    step_h = (1 if hours <= 6 else 4 if hours <= 24 else 12 if hours <= 72
              else 24 if hours <= 168 else 120)
    step = step_h * 3600
    lt = time.localtime(t0)
    midnight = int(t0) - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)
    tick = midnight
    while tick < t0:
        tick += step
    ticks = []
    while tick <= t1:
        ticks.append(tick)
        tick += step
    return ticks


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


def _legend(items, x0, x1, y, font, cols=3):
    """Lay legend items out as an equally-spaced grid (default 3 columns, no borders),
    filled row-major. Each item is (kind, color, dash, text) with kind 'line'
    (temperature trace) or 'box' (stripe). Returns (svg, y_after_last_row)."""
    sw, gap = 24, 7
    col_w = (x1 - x0) / cols
    row_h = font + 14
    out = []
    for i, (kind, color, dash, text) in enumerate(items):
        cx = x0 + (i % cols) * col_w
        cy = y + (i // cols) * row_h
        if kind == 'line':
            d = f' stroke-dasharray="{dash}"' if dash else ''
            out.append(f'<line x1="{cx:.1f}" y1="{cy}" x2="{cx + sw:.1f}" y2="{cy}" '
                       f'stroke="{color}" stroke-width="2.6"{d}/>')
        else:
            out.append(f'<rect x="{cx:.1f}" y="{cy - font * 0.55:.1f}" width="{sw}" '
                       f'height="{font * 0.8:.1f}" rx="2" fill="{color}"/>')
        out.append(f'<text x="{cx + sw + gap:.1f}" y="{cy + font * 0.35:.1f}" '
                   f'font-size="{font}" fill="{_COL["label"]}">{html.escape(text)}</text>')
    rows = (len(items) + cols - 1) // cols
    return ''.join(out), y + rows * row_h


def render_room_svg(room, data):
    """Render one room's history as a standalone inline SVG string."""
    t0, t1 = data['t_start'], data['now']
    x0, x1 = _L, _W - _R
    y0, y1 = _T, _H - _B
    temp, outdoor = data['temp'], data['outdoor']
    outdoor_ref = data.get('outdoor_ref') or []

    vals = [v for _, v in temp] + [v for _, v in outdoor] + [v for _, v in outdoor_ref]
    body = []
    if not vals:
        body.append(f'<text x="{_W/2}" y="{_H/2}" text-anchor="middle" '
                    f'fill="{_COL["label"]}" font-size="{_FS_EMPTY}">no temperature '
                    f'history for the selected window</text>')
    else:
        # y gridlines at round temperatures (nice-number axis), not the raw data edges
        vmin, vmax, vstep = _temp_axis(min(vals), max(vals))
        tv = vmin
        while tv <= vmax + 1e-9:
            yy = y1 - (tv - vmin) / (vmax - vmin) * (y1 - y0)
            body.append(f'<line x1="{x0}" y1="{yy:.1f}" x2="{x1}" y2="{yy:.1f}" '
                        f'stroke="{_COL["grid"]}"/>')
            body.append(f'<text x="{x0 - 7}" y="{yy + _FS * 0.35:.1f}" '
                        f'text-anchor="end" font-size="{_FS}" fill="{_COL["label"]}">'
                        f'{tv:g}°</text>')
            tv += vstep
        # x time ticks at even local-time boundaries (round hours/days), with a
        # vertical gridline each; the label format widens with the range.
        hrs = data['hours']
        tfmt = ('%H:%M' if hrs <= 24 else '%a %H:%M' if hrs <= 72
                else '%a' if hrs <= 168 else '%d.%m')
        for t in _time_ticks(t0, t1, hrs):
            xx = _x(t, t0, t1, x0, x1)
            body.append(f'<line x1="{xx:.1f}" y1="{y0}" x2="{xx:.1f}" y2="{y1}" '
                        f'stroke="{_COL["grid"]}"/>')
            body.append(f'<text x="{xx:.1f}" y="{y1 + _FS + 5:.0f}" '
                        f'text-anchor="middle" font-size="{_FS}" '
                        f'fill="{_COL["label"]}">{time.strftime(tfmt, time.localtime(t))}'
                        f'</text>')
        # damped HP-ref behind (faint dotted), real outdoor (dashed), room temp on top
        body.append(_polyline(outdoor_ref, t0, t1, vmin, vmax, x0, x1, y0, y1,
                              _COL['outdoor_ref'], dash='2 3'))
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
        ('line', _COL['outdoor'], '5 4', 'outdoor (real)'),
        # the damped HP-ref line is only drawn when damping is active (off -> it
        # just mirrors the real outdoor, so collect_room_history omits the series)
        *([('line', _COL['outdoor_ref'], '2 3', 'outdoor (HP ref)')]
          if outdoor_ref else []),
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
