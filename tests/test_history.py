"""Per-room history charts: slug/entity mapping, interval algebra, SVG (no DB)."""
import history


def test_slugify_handles_umlauts_and_spaces():
    assert history.slugify('Waschküche') == 'waschkuche'
    assert history.slugify('Arbeitszimmer Fenster') == 'arbeitszimmer_fenster'
    assert history.slugify('WC OG') == 'wc_og'
    assert history.slugify('Caro Fenster') == 'caro_fenster'


def test_room_entities_from_config_and_override():
    item = {'sensors': {'temperature': 'Arbeitszimmer Bewegungsmelder',
                        'windows': ['Arbeitszimmer Fenster']}}
    ent = history.room_entities('Arbeitszimmer', item)
    assert ent['temperature'] == 'arbeitszimmer_bewegungsmelder_temperature'
    assert ent['windows'] == ['arbeitszimmer_fenster_contact']
    assert ent['outdoor'] == history.HP_OUTDOOR_TEMP
    # override wins per field
    ent2 = history.room_entities('Arbeitszimmer', item,
                                 {'temperature': 'custom_temp'})
    assert ent2['temperature'] == 'custom_temp'
    # no configured sensor -> fall back to the TRV's local_temperature
    ent3 = history.room_entities('Schlafzimmer', {'sensors': {'windows': []}})
    assert ent3['temperature'] == 'schlafzimmer_thermostat_local_temperature'


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
    # heat pump cooling the whole window; window open in the middle
    responses = {
        "entity_id='temp_ent'": [[now - 3600, 24.0], [now - 1800, 23.0]],
        f"entity_id='{history.HP_OUTDOOR_TEMP}'": [[now - 3600, 30.0]],
        # compressor_activity: SELECT state ... -> string values
        f"SELECT state FROM \"state\" WHERE entity_id='{history.HP_ACTIVITY}' AND time>now()-6h":
            [[now - 3600, 'cooling'], [now, 'cooling']],
        "entity_id='win_ent'": [[now - 3000, 1], [now - 2000, 0]],
    }
    client = FakeInflux(responses)
    ent = {'temperature': 'temp_ent', 'outdoor': history.HP_OUTDOOR_TEMP,
           'activity': history.HP_ACTIVITY, 'windows': ['win_ent']}
    data = history.collect_room_history(client, ent, 6, now=now)
    assert data['temp'] and data['hp_cooling'] and not data['hp_heating']
    # the open-window span is cut out of the conditioned band
    assert any(a <= now - 3000 and b >= now - 3000 for a, b in data['window'])
    assert all(not (s < now - 2000 and e > now - 3000)
               for s, e in data['conditioned'])


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
