"""Per-room history charts: interval algebra, candidate resolution, SVG (no DB)."""
import history


def test_fold_intervals_runs_and_trailing_open():
    # value holds until the next sample; trailing 'on' clamps to t_end
    rows = [(0, 0), (10, 1), (20, 0), (30, 1)]
    assert history.fold_intervals(rows, 0, 40) == [(10, 20), (30, 40)]
    # seeded already-on at the window edge
    rows2 = [(0, 1), (15, 0)]
    assert history.fold_intervals(rows2, 0, 40) == [(0, 15)]


def test_subtract_and_intersect_intervals():
    base = [(0, 100)]
    assert history.subtract_intervals(base, [(20, 40)]) == [(0, 20), (40, 100)]
    assert history._intersect([(0, 50)], [(30, 80)]) == [(30, 50)]
    assert history.union_intervals([(0, 10), (5, 20)], [(30, 40)]) == [(0, 20), (30, 40)]


class FakeInflux(history.InfluxClient):
    """InfluxClient whose _fetch is a canned per-query lookup (never touches a DB)."""
    def __init__(self, responses):
        super().__init__()
        self._responses = responses

    def _fetch(self, q):
        if 'time<now()' in q:        # the "value before the window" seed query
            return []
        for needle, rows in self._responses.items():
            if needle in q:
                return rows
        return []


def test_collect_room_history_conditioned_excludes_window():
    now = 10_000
    # heat pump cooling the whole window; window open in the middle. The window's
    # named-slug entity has NO data; only its ieee-form candidate does — the
    # resolver must fall through to it (the Schlafzimmer case).
    responses = {
        "entity_id='temp_ent'": [[now - 3600, 24.0], [now - 1800, 23.0]],
        f"entity_id='{history.HP_OUTDOOR_TEMP}'": [[now - 3600, 30.0]],
        f"SELECT state FROM \"state\" WHERE entity_id='{history.HP_ACTIVITY}' AND time>now()-6h":
            [[now - 3600, 'cooling'], [now, 'cooling']],
        "entity_id='0xwin_contact'": [[now - 3000, 1], [now - 2000, 0]],
    }
    client = FakeInflux(responses)
    spec = {'temp': [['missing_temp', 'temp_ent']],   # one group; 2nd candidate has data
            'outdoor': history.HP_OUTDOOR_TEMP, 'activity': history.HP_ACTIVITY,
            'windows': [['win_named_contact', '0xwin_contact']]}
    data = history.collect_room_history(client, spec, 6, now=now)
    assert data['temp'] and data['hp_cooling'] and not data['hp_heating']
    # resolved temp via the 2nd candidate; window via its ieee-form candidate
    assert any(a <= now - 3000 and b >= now - 3000 for a, b in data['window'])
    assert all(not (s < now - 2000 and e > now - 3000)
               for s, e in data['conditioned'])


def test_stale_seed_is_ignored():
    """A pre-window 'open' that is older than SEED_MAX_AGE must NOT paint a phantom
    open band (lossy contact sensors drop their close event and get stuck 'open')."""
    now = 1_000_000
    t_start = now - 72 * 3600

    class SeedInflux(history.InfluxClient):
        def __init__(self, seed_age_h):
            super().__init__()
            self.seed_age_h = seed_age_h

        def _fetch(self, q):
            if 'time<now()' in q:                       # the seed query -> stale 'open'
                return [[t_start - self.seed_age_h * 3600, 1]]
            return [[now - 3600, 1], [now - 1800, 0]]   # a real 30-min open in-window

    # seed 5 days before the window -> dropped: only the real 30-min span remains
    band = SeedInflux(120).state_intervals_merged(['e'], 72, t_start, now)
    assert band == [(now - 3600, now - 1800)]
    # a fresh seed (2 h before window) is still honoured -> open from t_start
    band2 = SeedInflux(2).state_intervals_merged(['e'], 72, t_start, now)
    assert band2[0][0] == t_start


def test_long_open_span_is_dropped_as_artifact():
    """An in-window 'open' that never gets a close (dropped edge) folds into a giant
    span; spans longer than MAX_OPEN are hidden so week/month views stay clean."""
    now = 2_000_000
    t_start = now - 720 * 3600

    class Influx(history.InfluxClient):
        def _fetch(self, q):
            if 'time<now()' in q:
                return []
            # a stuck-open span (~19 h, dropped close) then a recent real 30-min open
            return [[t_start + 3600, 1], [t_start + 20 * 3600, 0],
                    [now - 3600, 1], [now - 1800, 0]]

    spec = {'temp': [], 'outdoor': None, 'activity': None,
            'windows': [['w']]}
    d = history.collect_room_history(Influx(), spec, 720, now=now)
    # the multi-week phantom is gone; only the real 30-min span remains
    assert d['window'] == [(now - 3600, now - 1800)]


def test_time_ticks_land_on_even_local_boundaries():
    import time
    # a window with an arbitrary edge time -> ticks must be round, not the edge
    t1 = int(time.mktime(time.strptime('2026-06-21 17:46:00', '%Y-%m-%d %H:%M:%S')))
    t0 = t1 - 24 * 3600
    ticks = history._time_ticks(t0, t1, 24)
    assert ticks, 'expected ticks in the window'
    for t in ticks:
        lt = time.localtime(t)
        assert lt.tm_min == 0 and lt.tm_sec == 0      # on the hour
        assert lt.tm_hour % 4 == 0                    # 24h range -> every 4h
    # daily range -> midnights
    for t in history._time_ticks(t0 - 6 * 86400, t1, 168):
        lt = time.localtime(t)
        assert lt.tm_hour == 0 and lt.tm_min == 0


def test_temp_axis_rounds_to_nice_bounds():
    vmin, vmax, step = history._temp_axis(24.1, 27.3)
    assert step == 1 and vmin == 24 and vmax == 28      # ticks 24,25,26,27,28
    assert all(float(t).is_integer() for t in
               [vmin + i * step for i in range(int((vmax - vmin) / step) + 1)])
    # wide range -> 5° steps
    _, _, step2 = history._temp_axis(17.2, 35.8)
    assert step2 == 5
    # flat data still yields a few gridlines around it
    lo, hi, st = history._temp_axis(25.5, 25.5)
    assert lo < 25.5 < hi and (hi - lo) >= 1.5 * st


def test_hold_to_edges_extends_recent_but_not_stale():
    now, t0 = 1_000_000, 1_000_000 - 24 * 3600
    # a recent series sitting inside the window -> held flat to both edges
    s = [(t0 + 3600, 22.0), (now - 3600, 24.0)]
    held = history.hold_to_edges(s, t0, now)
    assert held[0] == (t0, 22.0) and held[-1] == (now, 24.0)
    # last point far older than HOLD_MAX from `now` -> right edge NOT extended (gap)
    stale = [(t0 + 3600, 22.0), (now - 20 * 3600, 23.0)]
    held2 = history.hold_to_edges(stale, t0, now)
    assert held2[0] == (t0, 22.0)            # start gap is small -> extended
    assert held2[-1] == (now - 20 * 3600, 23.0)   # end left open (no flat to now)
    assert history.hold_to_edges([], t0, now) == []


def test_series_best_falls_back_to_richer_source():
    """A frozen/sparse room sensor (1 point) is beaten by the live TRV temp (many
    points) — series_best picks the richer source instead of the dead one."""
    now = 100_000

    class Influx(history.InfluxClient):
        def _fetch(self, q):
            if "entity_id='air'" in q:               # frozen air sensor: 1 point
                return [[now - 3600, 17.9]]
            if "entity_id='trv'" in q:               # live TRV: several points
                return [[now - 5400, 21.5], [now - 3600, 22.0], [now - 1800, 21.0]]
            return []

    c = Influx()
    best = c.series_best([['air'], ['trv']], 72)
    assert [v for _, v in best] == [21.5, 22.0, 21.0]      # TRV wins
    # if the air sensor were healthy (more points) it would win instead
    assert c.series_best([['trv'], ['nope']], 72) == c.series_merged(['trv'], 72)


def test_candidates_merge_across_a_rename():
    """When history is split across two entity ids (HA rename), the window band is the
    UNION of both candidates — not just the first one with data."""
    now = 100_000
    responses = {
        # old ieee entity holds the early part; new named entity the late part
        "entity_id='0xwin_contact'": [[now - 5000, 1], [now - 4000, 0]],
        "entity_id='win_named_contact'": [[now - 2000, 1], [now - 1000, 0]],
    }
    client = FakeInflux(responses)
    band = client.state_intervals_merged(
        ['win_named_contact', '0xwin_contact'], 24, now - 6 * 3600, now)
    # both open spans show up (early from ieee id, late from named id)
    assert any(abs(a - (now - 5000)) < 2 and abs(b - (now - 4000)) < 2 for a, b in band)
    assert any(abs(a - (now - 2000)) < 2 and abs(b - (now - 1000)) < 2 for a, b in band)


def test_render_room_svg_has_lines_and_stripes():
    now = 10_000
    data = {'now': now, 't_start': now - 6 * 3600, 'hours': 6,
            'temp': [(now - 3600, 24.0), (now - 60, 23.0)],
            'outdoor': [(now - 3600, 30.0), (now - 60, 31.0)],
            'hp_cooling': [(now - 3000, now - 1000)], 'hp_heating': [],
            'window': [(now - 2000, now - 1500)],
            'conditioned': [(now - 3000, now - 2000)]}
    svg = history.render_room_svg('Arbeitszimmer', data)
    assert svg.startswith('<svg') and svg.rstrip().endswith('</svg>')
    assert svg.count('<polyline') == 2          # room temp + outdoor reference
    # short stripe-row labels (kept narrow, ~as wide as the temp ticks)
    for short in ('>Cond<', '>HP<', '>Open<'):
        assert short in svg
    # ...spelled out in the legend below, with the temp traces
    for label in ('room temp', 'outdoor (HP ref)', 'Cond = conditioned',
                  'HP cooling', 'HP heating', 'Open = window open'):
        assert label in svg


def test_render_room_svg_empty_temp_is_graceful():
    now = 10_000
    data = {'now': now, 't_start': now - 6 * 3600, 'hours': 6,
            'temp': [], 'outdoor': [], 'hp_cooling': [], 'hp_heating': [],
            'window': [], 'conditioned': []}
    svg = history.render_room_svg('Leer', data)
    assert '<svg' in svg and 'no temperature history' in svg
