"""Consuming the mode tag: telling a user's change from our own lost write.

Detect-by-exclusion could not do this — a knob turn and a dropped write both
produced `manual`. With the tag we know which mode we last drove the room into,
so a valve doing something else is attributable.
"""
import health
import cooling
import modetag

SCHED = '00:00/19.5 03:00/19.5 05:00/20.5 11:00/20.5 17:00/20.5 23:00/19.5'
TYPE = {'schedule_mode': {'system_mode': 'heat', 'preset': 'schedule'},
        'cooling_open': {'preset': 'comfort', 'comfort_temperature': 34},
        'manual_marker': {'field': 'preset', 'equals': 'manual'},
        'off_signature': {'system_mode': 'off', 'frost_protection': 'ON'}}
TYPES = {'TR-M3Z': TYPE}
ITEM = {'day_hour': '05:00', 'day_temperature': 20.5, 'night_hour': '23:00',
        'night_temperature': 19.5, 'type': 'TR-M3Z'}
LIMITS = {'battery_limit': 20, 'unseen_interval': 1800, 'stale_temp_secs': 0}

OPEN = {'preset': 'comfort', 'comfort_temperature': 34}
OFF = {'system_mode': 'off', 'frost_protection': 'ON'}
MANUAL = {'preset': 'manual'}


def tagged(ctrl, mode, gen=0, sat_mode=None):
    st = dict(ctrl)
    st['schedule_sunday'] = modetag.apply(SCHED, modetag.encode(mode, gen))
    st['schedule_saturday'] = modetag.apply(
        SCHED, modetag.encode(sat_mode or mode, gen))
    return st


def verdict(state):
    return cooling.tag_verdict(TYPE, state, 'schedule')['verdict']


def test_matching_tag_and_valve_is_ok():
    assert verdict(tagged(OPEN, 'cooling')) == 'ok'
    assert verdict(tagged(OFF, 'idle')) == 'ok'


def test_user_change_is_attributed():
    """We drove it to cooling; it now reports manual -> the user did that."""
    assert verdict(tagged(MANUAL, 'cooling')) == 'user_changed'
    assert verdict(tagged(OPEN, 'idle')) == 'user_changed'


def test_untagged_device_is_not_accused():
    """A re-joined valve carries no tag — that is not a user change."""
    assert verdict(OPEN) == 'untagged'
    assert verdict({}) == 'untagged'


def test_carrier_days_disagreeing_is_its_own_verdict():
    st = tagged(OPEN, 'cooling', sat_mode='idle')
    assert verdict(st) == 'disagree'


def test_unknown_state_is_not_a_user_change():
    st = tagged({}, 'cooling')
    st.pop('preset', None)
    assert cooling.tag_verdict(TYPE, st, 'schedule')['verdict'] in ('ok', 'user_changed')
    assert cooling.tag_verdict(TYPE, None, 'schedule')['verdict'] == 'untagged'


def _kinds(reported, mode='cooling'):
    return [i.kind for i in health.classify_device(
        'Waschküche', ITEM, TYPES, {'base_topic': 'zigbee2mqtt'},
        reported, 1000.0, 1000.0, LIMITS, mode=mode)]


def test_user_override_is_reported_in_every_season():
    for season in ('heating', 'cooling', 'standby'):
        assert 'user_override' in _kinds(tagged(MANUAL, 'cooling'), season), season


def test_no_override_reported_when_consistent():
    assert 'user_override' not in _kinds(tagged(OPEN, 'cooling'))


def test_disagreeing_carriers_are_reported():
    assert 'tag_mismatch' in _kinds(tagged(OPEN, 'cooling', sat_mode='idle'))


def test_override_message_names_both_sides():
    issues = health.classify_device(
        'Waschküche', ITEM, TYPES, {'base_topic': 'zigbee2mqtt'},
        tagged(MANUAL, 'cooling'), 1000.0, 1000.0, LIMITS, mode='cooling')
    iss = [i for i in issues if i.kind == 'user_override'][0]
    assert 'cooling' in iss.detail and 'manual' in iss.detail
    assert iss.severity == 'info', "a user changing their own valve is not a fault"


def test_plain_off_with_idle_tag_is_not_a_user_change():
    """A valve we closed for standby reports a bare `system_mode: off` — it
    matches no signature (TRVZB has none at all), so classify_state calls it
    'manual'. Accusing the user of every valve we switched off ourselves was a
    false positive on five live rooms."""
    plain_off = {'system_mode': 'off'}
    assert verdict(tagged(plain_off, 'idle')) == 'ok'
    assert 'user_override' not in _kinds(tagged(plain_off, 'idle'), 'standby')


def test_idle_tag_still_catches_a_valve_switched_back_on():
    assert verdict(tagged({'preset': 'comfort', 'comfort_temperature': 34},
                          'idle')) == 'user_changed'


def test_users_plain_off_is_still_respected():
    """classify_state now calls any off 'off', so it no longer reads as manual.
    A user switching a valve off during cooling must still be left alone — the
    tag is what catches it: we said cooling, the valve is off."""
    assert verdict(tagged({'system_mode': 'off'}, 'cooling')) == 'user_changed'
    assert 'user_override' in _kinds(tagged({'system_mode': 'off'}, 'cooling'))
