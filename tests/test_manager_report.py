"""Manager status report + manual-override helpers (no MQTT connection)."""
import tempfile

import thermostat_monitor as tm

CFG = {
    'mqtt': {'base_topic': 'zigbee2mqtt'},
    'alerts': {'enabled': False},
    'season': {'mode': 'heating'},
    'thermostat_types': {
        'VNTH-T2_v2': {'schedule_mode': {'system_mode': 'heat', 'preset': 'schedule'},
                       'cooling_open': {'preset': 'comfort', 'comfort_temperature': 34},
                       'manual_marker': {'field': 'preset', 'equals': 'manual'}},
    },
    'thermostats': {
        'Bad OG': {'day_hour': '05:00', 'day_temperature': 21.5, 'night_hour': '23:00',
                   'night_temperature': 19.5, 'type': 'VNTH-T2_v2',
                   'sensors': {'temperature': 'Bad OG Luft', 'windows': ['Bad OG']}},
    },
}


def make_mgr(state_file=None):
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in CFG.items()}
    # Isolate persistence to a throwaway path so tests never touch real state.
    cfg['device_state_file'] = state_file or (
        tempfile.mkdtemp() + '/devices.json')
    return tm.Manager(cfg)


def test_device_state_persists_across_restart(tmp_path):
    sf = str(tmp_path / 'devices.json')
    mgr = make_mgr(sf)
    mgr.last_state['Bad OG'] = {'preset': 'schedule', 'battery': 55}
    mgr.last_seen['Bad OG'] = '2026-06-19T10:00:00'
    mgr.sensor_state['Bad OG Luft'] = {'temperature': 22.3, 'battery': 90}
    mgr.sensor_seen['Bad OG Luft'] = '2026-06-19T10:01:00'
    mgr._save_device_state()

    # a fresh manager (simulating restart) loads the saved readings
    mgr2 = make_mgr(sf)
    assert mgr2.last_state['Bad OG'] == {'preset': 'schedule', 'battery': 55}
    assert mgr2.last_seen['Bad OG'] == '2026-06-19T10:00:00'
    assert mgr2.sensor_state['Bad OG Luft']['temperature'] == 22.3


def test_restored_old_timestamp_gets_restart_grace():
    """A restored, long-stale last_seen must not immediately alert no_life_sign."""
    mgr = make_mgr()
    # last seen far in the past, but the device hasn't reported this session
    eff = mgr._effective_seen('2000-01-01T00:00:00')
    assert eff >= mgr.start_ts        # clamped up to startup -> full grace window


def test_manual_override_listed():
    mgr = make_mgr()
    mgr.last_state['Bad OG'] = {'preset': 'manual'}
    assert mgr.manual_overrides() == ['Bad OG']
    mgr.last_state['Bad OG'] = {'preset': 'schedule'}
    assert mgr.manual_overrides() == []


def test_manual_override_colored_and_reonboard_instructions():
    """A left-alone (manual) TRV gets a coloured state cell plus a section
    telling the operator how to re-onboard it (back to schedule mode)."""
    mgr = make_mgr()
    mgr.last_state['Bad OG'] = {'preset': 'manual'}
    d = mgr._report_data()

    # state cell coloured with the manual style
    ri = d['thermo']['rows'].index(next(r for r in d['thermo']['rows']
                                        if r[0] == 'Bad OG'))
    assert d['thermo']['styles'][ri].get('state') == mgr._CSS_MANUAL

    # re-onboard instructions point at the wrapper command, keyed by room
    assert d['override_head'] == 'Bad OG'
    assert d['override_rows'] == [('Bad OG', 'run: thermostat-reonboard "Bad OG"')]

    # surfaced in all three renderings
    assert 'Manual (left alone)' in mgr._render_text(d)
    assert 'thermostat-reonboard' in mgr._render_text(d)
    assert 'Manual (left alone)' in mgr._render_html(d)
    assert 'Manual (left alone)' in mgr._render_web(d)


def test_manual_override_section_absent_when_none():
    mgr = make_mgr()
    mgr.last_state['Bad OG'] = {'system_mode': 'heat', 'preset': 'schedule'}
    d = mgr._report_data()
    assert d['override_head'] is None
    assert 'Manual (left alone)' not in mgr._render_web(d)


def test_z2m_device_links_for_trv_and_sensor():
    """Each TRV/sensor name cell links to its z2m device page (keyed by ieee),
    opening in a new tab; the temp cell still links to the history chart."""
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in CFG.items()}
    cfg['mqtt'] = {'base_topic': 'zigbee2mqtt', 'broker': 'mqtt.lan'}
    cfg['device_state_file'] = tempfile.mkdtemp() + '/devices.json'
    mgr = tm.Manager(cfg)
    mgr.thermo_ieee['Bad OG'] = '0x1111'
    mgr.sensor_ieee['Bad OG Luft'] = '0x2222'
    mgr.last_state['Bad OG'] = {'preset': 'schedule'}
    mgr.sensor_state['Bad OG Luft'] = {'temperature': 21.0}

    d = mgr._report_data()
    # device-name links route through our /z2m?d=<ieee> endpoint (which logs the tap
    # and 302s to the frontend); the real target is built by z2m_target().
    assert d['thermo']['z2m'] == ['/z2m?d=0x1111']
    assert mgr.z2m_target('0x1111') == 'http://mqtt.lan:8080/#/device/0/0x1111/info'
    luft_i = d['sensors']['rows'].index(next(r for r in d['sensors']['rows']
                                             if r[0] == 'Bad OG Luft'))
    assert d['sensors']['z2m'][luft_i] == '/z2m?d=0x2222'

    html = mgr._render_web(d, link_rooms=True)
    # same-tab internal links (no target="_blank"): a direct in-page link to the
    # z2m hash route bounces to the device list on iOS Safari; the /z2m redirect
    # turns it into a fresh top-level load that resolves.
    assert 'href="/z2m?d=0x1111">' in html
    assert 'href="/z2m?d=0x2222">' in html
    assert 'target="_blank"' not in html
    assert 'href="/room?' in html          # temp cell still links to history


def test_z2m_link_omitted_when_ieee_unknown_or_disabled():
    mgr = make_mgr()                        # no broker -> base http://None:8080
    mgr.z2m_base = ''                       # explicitly disabled
    mgr.thermo_ieee['Bad OG'] = '0x1111'
    mgr.last_state['Bad OG'] = {'preset': 'schedule'}
    d = mgr._report_data()
    assert d['thermo']['z2m'] == [None]
    assert '/z2m?' not in mgr._render_web(d, link_rooms=True)


def test_window_ignore_warns_and_does_not_switch_off():
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in CFG.items()}
    cfg['window_control'] = {'enabled': True, 'act': True, 'ignore': True}
    cfg['web'] = {'enabled': True}
    cfg['device_state_file'] = tempfile.mkdtemp() + '/devices.json'
    mgr = tm.Manager(cfg)
    # Bad OG window is open
    mgr.sensor_state['Bad OG'] = {'contact': False}
    mgr.last_state['Bad OG'] = {'preset': 'comfort', 'comfort_temperature': 34}

    class C:
        def __init__(self): self.pub = []
        def publish(self, t, p, qos=0): self.pub.append((t, p))
    c = C()
    mgr._apply_window_control('Bad OG', c)
    assert c.pub == []                                 # never switched off
    assert 'Bad OG' not in mgr.window_off
    d = mgr._report_data()
    assert d['warn_line'] and 'IGNORED' in d['warn_line'] and 'Bad OG' in d['warn_line']
    assert '⚠' in mgr.status_report()                  # text report carries it
    mgr._last_report = d
    assert 'IGNORED' in mgr.web_page()                  # web banner


def test_seen_style_highlights_stale_and_warming_page_polls_fast():
    import time
    mgr = make_mgr()
    fresh = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime())
    assert mgr._seen_style(fresh) is None                 # recent -> no highlight
    assert mgr._seen_style('2026-01-01T00:00:00') == mgr._CSS_WARN   # >4h -> stale
    assert mgr._seen_style(None) == mgr._CSS_WARN          # never seen -> stale
    # the warming-up (no data) page polls fast so it picks up data quickly
    warm = tm.Manager._render_web(None, refresh=60)
    assert 'content="3"' in warm and 'Starting up' in warm
    # a real page keeps the configured refresh
    assert 'content="60"' in tm.Manager._render_web({'n_alert': 0, 'n_info': 0,
        'overall': 'ok', 'when': 'now', 'mode': 'cooling', 'hp_line': None,
        'manual_line': None, 'thermo': {'headers': [], 'rows': []},
        'sensors': None, 'issues': []}, refresh=60)


def test_seen_reflects_oldest_displayed_datum_when_temp_frozen():
    """A TRV that keeps pinging (fresh last_seen) but stopped sending fresh
    temperature reads days ago must show 'seen' as the temperature's age, not 0m."""
    import time
    mgr = make_mgr()
    fresh = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime())
    stale = '2026-06-19T10:00:00'                 # days old
    # TRV is the temp source (no fresh air sensor); device pings now, temp frozen long ago
    mgr.last_state['Bad OG'] = {'local_temperature': 20.5}
    mgr.last_seen['Bad OG'] = fresh
    mgr.trv_temp_seen['Bad OG'] = stale
    assert mgr._room_temp_seen('Bad OG') == stale
    # 'seen' = oldest of device-liveness and temperature age -> the stale temp wins
    assert mgr._oldest_iso(fresh, stale) == stale
    assert mgr._seen_style(mgr._oldest_iso(fresh, stale)) == mgr._CSS_WARN


def test_track_temp_change_stamps_only_on_change():
    import time
    mgr = make_mgr()
    store = {}
    mgr._track_temp_change(store, 'k', None, {'local_temperature': 20.5}, 'local_temperature')
    first = store['k']
    assert first is not None
    # same value re-published -> stamp unchanged (age grows)
    mgr._track_temp_change(store, 'k', {'local_temperature': 20.5},
                           {'local_temperature': 20.5}, 'local_temperature')
    assert store['k'] == first
    # value changed -> stamp refreshed
    store['k'] = '2026-06-19T10:00:00'
    mgr._track_temp_change(store, 'k', {'local_temperature': 20.5},
                           {'local_temperature': 21.0}, 'local_temperature')
    assert tm.parse_iso(store['k']) > tm.parse_iso('2026-06-19T10:00:00')


def test_temp_seen_stamps_persist_across_restart(tmp_path):
    sf = str(tmp_path / 'devices.json')
    mgr = make_mgr(sf)
    mgr.last_state['Bad OG'] = {'local_temperature': 20.5}
    mgr.sensor_state['Bad OG Luft'] = {'temperature': 22.0}
    mgr.trv_temp_seen['Bad OG'] = '2026-06-19T10:00:00'
    mgr.sensor_temp_seen['Bad OG Luft'] = '2026-06-19T10:01:00'
    mgr._save_device_state()
    mgr2 = make_mgr(sf)                            # simulated restart
    assert mgr2.trv_temp_seen['Bad OG'] == '2026-06-19T10:00:00'
    assert mgr2.sensor_temp_seen['Bad OG Luft'] == '2026-06-19T10:01:00'


def test_room_temp_falls_back_to_trv_when_air_sensor_stale():
    import time
    mgr = make_mgr()                                  # Bad OG sensor = "Bad OG Luft"
    mgr.last_state['Bad OG'] = {'local_temperature': 22.4}
    # fresh air sensor -> used
    mgr.sensor_state['Bad OG Luft'] = {'temperature': 17.9}
    mgr.sensor_seen['Bad OG Luft'] = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime())
    assert mgr._room_temp_state('Bad OG')['temperature'] == 17.9
    # stale air sensor (seen long ago) -> fall back to the TRV's local_temperature
    mgr.sensor_seen['Bad OG Luft'] = '2026-01-01T00:00:00'
    assert mgr._room_temp_state('Bad OG')['temperature'] == 22.4


def test_sensor_and_thermostat_namespaces_dont_collide():
    """A contact sensor named 'Bad OG' must not clobber the 'Bad OG' thermostat."""
    mgr = make_mgr()
    mgr.last_state['Bad OG'] = {'preset': 'schedule', 'battery': 50}
    mgr.sensor_state['Bad OG'] = {'contact': False, 'battery': 80}
    assert mgr.last_state['Bad OG']['battery'] == 50
    assert mgr.sensor_state['Bad OG']['battery'] == 80


class _FakeClient:
    def __init__(self):
        self.subs, self.unsubs = [], []

    def subscribe(self, t):
        self.subs.append(t)

    def unsubscribe(self, t):
        self.unsubs.append(t)


def test_registry_backfills_ieee_and_resubscribes_on_rename():
    """bridge/devices back-fills ieee for name-only refs, and a z2m rename moves the
    device's subscription to its new topic without a restart."""
    mgr = make_mgr()                       # CFG refs Bad OG by friendly name (no ieee)
    c = _FakeClient()
    payload = [
        {'ieee_address': '0xTRV', 'friendly_name': 'Bad OG Thermostat'},
        {'ieee_address': '0xTEMP', 'friendly_name': 'Bad OG Luft'},
        {'ieee_address': '0xWIN', 'friendly_name': 'Bad OG'},
    ]
    mgr._on_registry(payload, c)
    assert mgr.thermo_ieee['Bad OG'] == '0xtrv'        # back-filled (lowercased)
    assert mgr.sensor_ieee['Bad OG Luft'] == '0xtemp'

    # rename the TRV in z2m -> daemon re-subscribes the new topic, drops the old
    c.subs.clear()
    mgr._on_registry([{'ieee_address': '0xTRV',
                       'friendly_name': 'Bad OG Thermostat NEU'}], c)
    base = mgr.base
    assert f"{base}/Bad OG Thermostat NEU" in c.subs
    assert f"{base}/Bad OG Thermostat" in c.unsubs
    assert mgr.thermo_topic.get(f"{base}/Bad OG Thermostat NEU") == 'Bad OG'


def test_mode_change_notifies_manual_valves():
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in CFG.items()}
    cfg['manual_thermostats'] = ['Keller', 'Gäste WC']
    mgr = tm.Manager(cfg)
    sent = []
    mgr.alerter.enabled = True
    mgr.alerter._sender = lambda subj, body, html=None: sent.append((subj, body)) or True

    mgr._notify_mode_change('heating', 'cooling')
    assert len(sent) == 1
    assert 'cooling' in sent[0][0]
    assert 'Keller' in sent[0][1] and 'OPEN' in sent[0][1]


def test_mode_change_silent_without_manual_valves():
    mgr = make_mgr()
    sent = []
    mgr.alerter.enabled = True
    mgr.alerter._sender = lambda subj, body, html=None: sent.append((subj, body)) or True
    mgr._notify_mode_change('cooling', 'heating')
    assert sent == []


def test_status_report_contains_overview():
    mgr = make_mgr()
    mgr.last_state['Bad OG'] = {'preset': 'manual', 'battery': 8}
    mgr.sensor_state['Bad OG Luft'] = {'temperature': 20.0}
    report = mgr.status_report()
    assert 'Thermostat status' in report
    assert 'Bad OG' in report and 'MANUAL' in report
    assert 'Mode' in report and 'heating' in report
    # battery filled, missing fields render as a dash rather than '?'
    assert '8%' in report and '—' in report


def test_state_uses_our_vocabulary_and_uniform_setpoint_in_header():
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in CFG.items()}
    cfg['season'] = {'mode': 'cooling', 'cool_target': 21}
    cfg['device_state_file'] = tempfile.mkdtemp() + '/devices.json'
    mgr = tm.Manager(cfg)
    # cooling-open signature -> classified as our 'open', not the raw 'comfort'
    mgr.last_state['Bad OG'] = {'preset': 'comfort', 'comfort_temperature': 34}
    d = mgr._report_data()
    assert d['set_line'] == '21°C'                 # lifted to a header line
    assert 'set' not in d['thermo']['headers']      # ...and dropped as a column
    assert d['thermo']['rows'][0][1] == 'open'      # our state, not 'comfort'
    report = mgr.status_report()
    assert 'Set point: 21°C (all rooms)' in report


def test_per_room_setpoints_fold_when_rooms_differ():
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in CFG.items()}
    cfg['web'] = {'enabled': True}
    cfg['thermostats'] = dict(cfg['thermostats'])
    # day == night so the range is deterministic regardless of the clock
    cfg['thermostats']['Bad OG'] = dict(cfg['thermostats']['Bad OG'],
                                        day_temperature=21.5, night_temperature=21.5)
    cfg['thermostats']['WC OG'] = {
        'day_hour': '05:00', 'day_temperature': 18.0, 'night_hour': '23:00',
        'night_temperature': 18.0, 'type': 'VNTH-T2_v2',
        'sensors': {'temperature': 'WC OG Luft'}}
    cfg['device_state_file'] = tempfile.mkdtemp() + '/devices.json'
    mgr = tm.Manager(cfg)   # per-room schedule points differ (Bad OG 21.5, WC OG 18)
    d = mgr._report_data()
    # never a table column now; uniform line absent, range summary + per-room detail
    assert 'set' not in d['thermo']['headers']
    assert d['set_line'] is None
    assert d['set_head'] == '18–21.5°C'
    assert ('WC OG', '18°C') in d['set_rows'] and ('Bad OG', '21.5°C') in d['set_rows']
    # web: a collapsed fold; text/mail: a compact line
    mgr._last_report = d
    assert '<details class="fold"><summary><span class="k">Set point</span> 18–21.5°C' \
        in mgr.web_page()
    assert 'Set points (18–21.5°C):' in mgr.status_report()


def test_web_page_renders_full_document():
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in CFG.items()}
    cfg['season'] = {'mode': 'cooling', 'cool_target': 21}
    cfg['web'] = {'enabled': True, 'refresh': 45}
    cfg['device_state_file'] = tempfile.mkdtemp() + '/devices.json'
    mgr = tm.Manager(cfg)
    mgr.last_state['Bad OG'] = {'preset': 'comfort', 'comfort_temperature': 34}
    # before the first eval pass the page is a placeholder, not a crash
    warm = mgr.web_page()
    assert '<!doctype html>' in warm and 'Starting up' in warm
    # after a snapshot is cached, the real page renders
    mgr._last_report = mgr._report_data()
    page = mgr.web_page()
    assert '<title>Klima Status' in page
    assert 'Bad OG' in page and 'open' in page
    assert 'content="45"' in page          # honours the configured refresh
    assert 'class="pill' in page
    # the room's temperature links to its history chart
    assert 'href="/room?name=Bad+OG' in page
    # project icon: favicon + header logo + iOS home-screen icon (coding standard §5)
    assert 'rel="icon"' in page and 'src="/logo.svg"' in page
    assert 'rel="apple-touch-icon" href="/apple-touch-icon.png"' in page
    assert mgr.logo_svg().lstrip().startswith('<?xml') or '<svg' in mgr.logo_svg()


def test_hp_overview_is_collapsed_details():
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in CFG.items()}
    cfg['web'] = {'enabled': True}
    cfg['device_state_file'] = tempfile.mkdtemp() + '/devices.json'
    mgr = tm.Manager(cfg)
    hp = {'mode': 'cooling', 'active': True,
          'telemetry': {'vorlauf': 14.8, 'ruecklauf': 16.0, 'power': 691},
          'raw': {'hpactivity': 'cooling'}}
    mgr._last_report = mgr._report_data(mode='cooling', hp=hp, issues=[])
    page = mgr.web_page()
    # summary (first line) always visible; telemetry in a collapsed (no `open`) table
    assert '<details class="fold"><summary>' in page
    assert '<details class="fold" open' not in page
    assert 'cooling season — cooling (running)' in page
    assert 'Flow (Vorlauf)' in page and '14.8°C' in page


def test_open_windows_overview_is_collapsed_with_room_summary():
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in CFG.items()}
    cfg['web'] = {'enabled': True}
    cfg['device_state_file'] = tempfile.mkdtemp() + '/devices.json'
    mgr = tm.Manager(cfg)
    mgr.window_off = {'Waschküche': '2026-06-21T15:13:42',
                      'Bad OG': '2026-06-21T15:20:00'}
    mgr._last_report = mgr._report_data(mode='cooling', hp=None, issues=[])
    page = mgr.web_page()
    # summary lists which rooms (sorted); ages live in the collapsed detail table
    assert '<details class="fold"><summary><span class="k">Off (window open)' in page
    assert 'Bad OG, Waschküche' in page          # summary = room names
    assert '<table class="fold-tbl">' in page     # per-room ages in the detail


def test_room_page_toggles_ranges_and_no_footer(monkeypatch):
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in CFG.items()}
    cfg['web'] = {'enabled': True}
    cfg['device_state_file'] = tempfile.mkdtemp() + '/devices.json'
    mgr = tm.Manager(cfg)
    import history
    monkeypatch.setattr(history.InfluxClient, '_fetch', lambda self, q: [])
    page = mgr.room_page('Bad OG', 24)
    assert '<svg' in page and 'Bad OG' in page
    assert 'href="/"' in page                              # "all rooms" link (top)
    assert '← back to status' not in page                 # bottom footer removed
    # all five ranges incl. week + month, with friendly labels
    for h, lbl in ((6, '6h'), (72, '3d'), (168, '1w'), (720, '1mo')):
        assert f'href="/room?name=Bad+OG&hours={h}">{lbl}</a>' in page
    assert mgr.room_page('Nope', 24) is None              # unknown room rejected


def test_hp_line_shows_current_activity():
    mgr = make_mgr()
    hp = {'mode': 'cooling', 'active': True,
          'telemetry': {'vorlauf': 22.1, 'ruecklauf': 55.4},
          'raw': {'hpactivity': 'hot water'}}
    d = mgr._report_data(mode='cooling', hp=hp, issues=[])
    # the DHW interlude is labelled so a hot return doesn't look like a fault
    assert d['hp_line'] == ('cooling season — hot water (running): '
                            'vorlauf 22.1°C, ruecklauf 55.4°C')


def test_hp_line_without_activity_field():
    mgr = make_mgr()
    hp = {'mode': 'heating', 'active': False,
          'telemetry': {'vorlauf': 35.0}, 'raw': {}}
    d = mgr._report_data(mode='heating', hp=hp, issues=[])
    assert d['hp_line'] == 'heating (idle): vorlauf 35°C'


def test_report_data_reuses_passed_issues():
    # passing issues must skip collect_issues (which records history as a side
    # effect) — verify no history is recorded when issues are supplied.
    mgr = make_mgr()
    mgr.last_state['Bad OG'] = {'preset': 'schedule'}
    d = mgr._report_data(mode='heating', hp=None, issues=[])
    assert d['n_alert'] == 0 and d['n_info'] == 0
    assert not mgr.history            # collect_issues was not called


def test_sensor_value_shows_humidity_and_dew_point():
    mgr = make_mgr()
    mgr.sensor_state['Bad OG Luft'] = {'temperature': 22.3, 'humidity': 55}
    report = mgr.status_report()
    assert '22.3°C' in report and '55%RH' in report
    assert 'dp 12.8°C' in report          # Magnus dew point for 22.3°C/55%RH


def test_hp_line_shows_dew_point_limit():
    mgr = make_mgr()
    hp = {'mode': 'cooling', 'active': True,
          'telemetry': {'vorlauf': 18.0},
          'raw': {'dewtemperature': 16.7, 'rftemp': 28.2, 'airhumidity': 50}}
    d = mgr._report_data(mode='cooling', hp=hp, issues=[])
    # the pump's dew point with the temp+humidity it derived it from
    assert 'dew 16.7°C (ems-esp 28.2°C 50%RH)' in d['hp_line']


def _seed_history(mgr, name, temps, step_min=5):
    """Push (ts, running_state, temp) samples spaced step_min apart, oldest first."""
    import time
    now = time.time()
    n = len(temps)
    for i, t in enumerate(temps):
        ts = now - (n - 1 - i) * step_min * 60
        mgr.history[name].append((ts, 'cool', t))


def test_improving_within_grace_is_quiet():
    mgr = make_mgr()
    # only 15 min of history (< 30 min grace) -> too soon to judge
    _seed_history(mgr, 'Bad OG', [26.0, 25.8, 25.6, 25.4])  # 4 * 5 min = 15 min
    assert mgr._temp_improving('Bad OG', 21.0, 'cooling') is True


def test_fast_enough_cooling_stays_quiet():
    mgr = make_mgr()
    # 60 min span, fell 1.2 °C -> 1.2 °C/h >= required 1 °C/h
    _seed_history(mgr, 'Bad OG', [26.2, 25.9, 25.6, 25.3, 25.2, 25.1, 25.0],
                  step_min=10)
    assert mgr._temp_improving('Bad OG', 21.0, 'cooling') is True


def test_too_slow_cooling_escalates():
    mgr = make_mgr()
    # 60 min span, fell only 0.3 °C -> 0.3 °C/h < required 1 °C/h -> loud
    _seed_history(mgr, 'Bad OG', [26.0, 25.95, 25.9, 25.85, 25.8, 25.75, 25.7],
                  step_min=10)
    assert mgr._temp_improving('Bad OG', 21.0, 'cooling') is False


def test_cooling_not_open_is_reported():
    import time
    mgr = make_mgr()
    # Bad OG (VNTH-T2_v2) drifted back to its schedule preset in cooling mode
    mgr.last_state['Bad OG'] = {'preset': 'schedule', 'comfort_temperature': 34}
    issues = mgr.collect_issues('cooling', time.time(), time.localtime(), None)
    keys = [i.key for i in issues]
    assert 'Bad OG:notopen' in keys
    iss = next(i for i in issues if i.key == 'Bad OG:notopen')
    assert iss.severity == 'info'      # report-only, not a loud alert


def test_cooling_open_room_not_flagged():
    import time
    mgr = make_mgr()
    mgr.last_state['Bad OG'] = {'preset': 'comfort', 'comfort_temperature': 34}
    issues = mgr.collect_issues('cooling', time.time(), time.localtime(), None)
    assert 'Bad OG:notopen' not in [i.key for i in issues]


def test_cooling_off_room_not_flagged_as_drift():
    import time
    mgr = make_mgr()
    # Switched off (e.g. window open) is intended, not a drift -> no notopen flag
    mgr.last_state['Bad OG'] = {'system_mode': 'off', 'preset': 'schedule',
                                'comfort_temperature': 34}
    issues = mgr.collect_issues('cooling', time.time(), time.localtime(), None)
    assert 'Bad OG:notopen' not in [i.key for i in issues]


def test_status_report_html_has_scrollable_tables():
    mgr = make_mgr()
    mgr.last_state['Bad OG'] = {'preset': 'manual', 'battery': 8}
    html = mgr.status_report_html()
    assert '<table' in html and 'overflow-x:auto' in html
    assert 'Bad OG' in html
    # HTML is escaped (no raw angle brackets injected from data)
    assert '<script' not in html


def test_html_color_cues():
    # cooling mode so "above target" is the problematic direction
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in CFG.items()}
    cfg['season'] = {'mode': 'cooling', 'cool_target': 21}
    cfg['device_state_file'] = tempfile.mkdtemp() + '/devices.json'
    mgr = tm.Manager(cfg)
    mgr.last_state['Bad OG'] = {'preset': 'comfort', 'battery': 8}     # low battery
    mgr.sensor_state['Bad OG Luft'] = {'temperature': 26.0}           # >21+1.5 -> hot
    mgr.sensor_state['Bad OG'] = {'contact': False}                   # window open
    html = mgr.status_report_html()
    assert tm.Manager._CSS_BAD in html      # low battery coloured
    assert tm.Manager._CSS_HOT in html      # too-warm temp coloured
    assert tm.Manager._CSS_WARN in html     # open window coloured
