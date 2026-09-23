"""The season follows what the heat pump actually does.

hc1 `hpmode` is the pump's main switch and bounds the season (with `heating`
the house never goes into cooling, however warm the afternoon); hc1
`hpoperatingstate` says what the circuit does now. In `heating & cooling` the
pump flipped heating -> off -> cooling twice a day in September, with ~1 h
`off` at each changeover, so an idle pump holds the previous season instead of
re-driving every valve, and only a long idle spell means standby.
`hpcooling` is useless for this: it lags the switch by `cooloffdelay` (72 h).
"""
import tempfile

import cooling
import heatpump
import thermostat_monitor as tm
import thermostat_scheduler as ts

SEASON = {'mode': 'auto', 'source': 'heatpump', 'standby_after_hours': 24,
          'manual_reminder_after_hours': 24}


def pump(hpmode='heating & cooling', state='heating', coolstart=20, outdoor=10,
         hpcooling='on'):
    thermo = {'hc1': {'hpmode': hpmode, 'hpoperatingstate': state,
                      'coolstart': coolstart, 'hpcooling': hpcooling}}
    return heatpump.parse({'outdoortemp': outdoor}, thermo,
                          {'fields': {'outdoor': 'outdoortemp'},
                           'cooling_when': [{'field': 'hpcooling', 'equals': 'on'}]})


def mode(hp, last=None, idle=None):
    outdoor = hp['telemetry'].get('outdoor')
    return cooling.desired_mode(SEASON, hp, outdoor, last, pump_idle_secs=idle)


def test_parse_reads_switch_and_state_not_hpcooling():
    hp = pump(hpmode='heating', state='heating', hpcooling='on')
    assert hp['allowed'] == ('heating',) and hp['state'] == 'heating'
    # hpcooling still 'on' (cooloffdelay) must not make this cooling
    assert hp['cooling'] is False and hp['mode'] == 'heating'
    assert pump(state='cooling')['mode'] == 'cooling'


def test_active_state_is_taken_as_is():
    assert mode(pump(state='heating')) == 'heating'
    assert mode(pump(state='cooling')) == 'cooling'


def test_switch_heating_never_cools():
    # cooling winding down after the switch went to heating, on a hot day
    assert mode(pump(hpmode='heating', state='cooling', outdoor=30),
                last='cooling') == 'heating'
    assert mode(pump(hpmode='heating', state='off', outdoor=30)) == 'heating'


def test_switch_off_is_standby():
    assert mode(pump(hpmode='off', state='off'), last='heating') == 'standby'


def test_idle_gap_holds_previous_season():
    assert mode(pump(state='off'), last='cooling', idle=3600) == 'cooling'
    assert mode(pump(state='off'), last='heating', idle=3600) == 'heating'


def test_long_idle_becomes_standby_and_stays_until_pump_runs():
    assert mode(pump(state='off'), last='heating', idle=25 * 3600) == 'standby'
    assert mode(pump(state='off'), last='standby', idle=None) == 'standby'
    assert mode(pump(state='heating'), last='standby') == 'heating'


def test_idle_without_history_uses_coolstart():
    assert mode(pump(state='off', outdoor=25, coolstart=20)) == 'cooling'
    assert mode(pump(state='off', outdoor=12, coolstart=20)) == 'heating'


def test_telemetry_gap_holds_last_mode():
    assert cooling.desired_mode(SEASON, None, None, 'cooling') == 'cooling'


def test_legacy_pump_without_fields_uses_cooling_flag():
    assert cooling.desired_mode(SEASON, {'cooling': True}) == 'cooling'
    assert cooling.desired_mode(SEASON, {'cooling': False}) == 'heating'


# ---- monitor bookkeeping ---------------------------------------------------

def make_mgr(manual=('Keller',), **season):
    cfg = {'mqtt': {'base_topic': 'zigbee2mqtt'}, 'thermostats': {},
           'alerts': {'enabled': True, 'state_file': tempfile.mkdtemp() + '/a.json'},
           'season': dict(SEASON, **season), 'manual_thermostats': list(manual),
           'device_state_file': tempfile.mkdtemp() + '/devices.json'}
    mgr = tm.Manager(cfg)
    sent = []
    mgr.alerter.enabled = True
    mgr.alerter._sender = lambda subj, body, html=None: sent.append(subj) or True
    return mgr, sent


def test_idle_clock_starts_and_clears():
    mgr, _ = make_mgr()
    mgr._track_pump_idle(pump(state='off'), 100.0)
    mgr._track_pump_idle(pump(state='off'), 200.0)
    assert mgr._pump_idle_since == 100.0
    # cooling the switch no longer permits counts as idle too
    mgr._track_pump_idle(pump(hpmode='heating', state='cooling'), 300.0)
    assert mgr._pump_idle_since == 100.0
    mgr._track_pump_idle(pump(state='heating'), 400.0)
    assert mgr._pump_idle_since is None


def test_manual_valve_reminder_is_immediate_by_default():
    mgr, sent = make_mgr(manual_reminder_after_hours=0)
    mgr._track_mode('heating', 0)                # baseline, no mail
    mgr._track_mode('heating', 3600)
    assert sent == []
    mgr._track_mode('cooling', 7200)
    assert len(sent) == 1 and 'cooling' in sent[0]
    mgr._track_mode('cooling', 10800)
    assert len(sent) == 1                        # once per change
    mgr._track_mode('heating', 14400)
    assert len(sent) == 2 and 'heating' in sent[1]


def test_manual_valve_reminder_can_wait_for_a_stable_season():
    mgr, sent = make_mgr()
    h = 3600
    mgr._track_mode('heating', 0)                # baseline, no mail
    mgr._track_mode('cooling', 10 * h)           # daily flip ...
    mgr._track_mode('heating', 20 * h)           # ... and back: nothing
    mgr._track_mode('heating', 43 * h)
    assert sent == []
    mgr._track_mode('cooling', 50 * h)
    mgr._track_mode('cooling', 73 * h)
    assert sent == []                            # 23 h: not yet
    mgr._track_mode('cooling', 74 * h)
    assert len(sent) == 1 and 'cooling' in sent[0]
    mgr._track_mode('cooling', 100 * h)
    assert len(sent) == 1                        # once per season change


# ---- CLI follows the daemon ------------------------------------------------

def test_cli_adopts_daemon_season(monkeypatch):
    monkeypatch.setattr(ts, 'query_monitor', lambda *a, **k: {})
    cfg = {'heatpump': {'enabled': True, 'boiler_topic': 'b', 'thermostat_topic': 't',
                        'fields': {'outdoor': 'outdoortemp'}},
           'season': dict(SEASON)}

    class C:
        def subscribe(self, *a, **k):
            pass
    responses = {'b': {'outdoortemp': 25},
                 't': {'hc1': {'hpmode': 'heating & cooling', 'hpoperatingstate': 'off',
                               'coolstart': 20}},
                 'thermostat_monitor/_season': {'mode': 'heating'}}
    m, _hp, _c = ts.detect_mode(cfg, C(), {'responses': responses}, 0)
    assert m == 'heating'          # stateless read would say cooling (25 > 20)
    del responses['thermostat_monitor/_season']
    m, _hp, _c = ts.detect_mode(cfg, C(), {'responses': responses}, 0)
    assert m == 'cooling'
