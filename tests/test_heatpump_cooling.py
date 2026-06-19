import heatpump
import cooling

HP_CFG = {
    'fields': {'vorlauf': 'curflowtemp', 'ruecklauf': 'rettemp',
               'outdoor': 'outdoortemp', 'pressure': 'syspress'},
    'cooling_when': [
        {'field': 'coolingon', 'equals': 'on'},
        {'field': 'hp4way', 'contains': 'cooling'},
    ],
}

BOILER = {'curflowtemp': 18.3, 'rettemp': 18.5, 'outdoortemp': 30.3,
          'syspress': 1.8, 'hp4way': 'cooling & defrost'}
THERMO = {'hc1': {'coolingon': 'off', 'hpmode': 'heating & cooling'}}


def test_parse_detects_cooling_via_hp4way():
    state = heatpump.parse(BOILER, THERMO, HP_CFG)
    assert state['cooling'] is True
    assert state['mode'] == 'cooling'
    assert state['telemetry']['vorlauf'] == 18.3


def test_no_cooling_when_signals_off():
    boiler = dict(BOILER, hp4way='heating')
    thermo = {'hc1': {'coolingon': 'off'}}
    state = heatpump.parse(boiler, thermo, HP_CFG)
    assert state['cooling'] is False
    assert state['mode'] == 'heating'


def test_check_bounds_flags_low_cooling_flow():
    tele = {'vorlauf': 5.0, 'pressure': 1.8}
    bounds = {'cooling': {'vorlauf': [8, 25], 'pressure': [1.0, 2.5]}}
    out = heatpump.check_bounds(tele, 'cooling', bounds)
    assert any(f == 'vorlauf' for f, *_ in out)
    assert not any(f == 'pressure' for f, *_ in out)


def test_desired_mode_forced_and_auto():
    assert cooling.desired_mode({'mode': 'cooling'}, None) == 'cooling'
    assert cooling.desired_mode({'mode': 'auto', 'source': 'heatpump'},
                                {'cooling': True}) == 'cooling'
    assert cooling.desired_mode({'mode': 'auto', 'source': 'heatpump'},
                                {'cooling': False}) == 'heating'
    assert cooling.desired_mode({'mode': 'auto', 'source': 'config'}, None) == 'heating'


def test_manual_override_detection():
    vnth = {'manual_marker': {'field': 'preset', 'equals': 'manual'}}
    assert cooling.is_manual_override(vnth, {'preset': 'manual'})
    assert not cooling.is_manual_override(vnth, {'preset': 'schedule'})


def test_open_and_restore_payloads():
    tcfg = {'cooling_open': {'preset': 'comfort', 'comfort_temperature': 30},
            'cooling_restore': {'preset': 'schedule'}}
    assert cooling.build_open_payload(tcfg) == {'preset': 'comfort', 'comfort_temperature': 30}
    assert cooling.build_restore_payload(tcfg) == {'preset': 'schedule'}
    assert cooling.build_open_payload({}) is None
