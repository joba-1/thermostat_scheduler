"""The mode tag carried in the weekly schedule.

The carrier is addressed by *position* (2nd entry of Saturday/Sunday), never by
the literal time: `generate_schedule_string` derives interior points from each
room's day/night hours, so Waschküche lands on 03:00 while Julians lands on
03:30. Encoding by time would silently miss half the house.
"""
import common
import modetag

SUN = '00:00/19.5 03:00/19.5 05:00/21.5 11:00/21.5 17:00/21.5 23:00/19.5'
HALF = '00:00/20.5 03:30/20.5 06:00/22.5 11:30/22.5 17:30/22.5 23:00/20.5'


def test_encode_decode_roundtrip():
    for mode in ('heating', 'cooling', 'idle', 'reserved'):
        for gen in range(modetag.GENERATIONS):
            assert modetag.decode(modetag.encode(mode, gen)) == (mode, gen)


def test_tag_stays_in_its_reserved_minute_range():
    vals = [modetag.encode(m, g) for m in modetag.MODES
            for g in range(modetag.GENERATIONS)]
    assert min(vals) >= modetag.TAG_BASE and max(vals) <= 59
    assert len(set(vals)) == len(modetag.MODES) * modetag.GENERATIONS


def test_natural_schedule_times_are_not_mistaken_for_a_tag():
    """generate_schedule_string rounds interior points to :00 or :30. With a
    0-based encoding those decoded as heating/gen0 and idle/gen7 on live rooms —
    every untagged valve looked tagged."""
    for natural in (0, 30, 15, 31):
        assert modetag.decode(natural) is None, natural
    assert modetag.decode(None) is None


def test_apply_touches_only_the_carrier_entry():
    minutes = modetag.encode('cooling', 3)
    out = modetag.apply(SUN, minutes)
    assert out.split()[0] == '00:00/19.5'          # entry 1 untouched
    assert out.split()[2:] == SUN.split()[2:]      # entries 3..6 untouched
    assert out.split()[1] == f'03:{minutes:02d}/19.5'


def test_carrier_keeps_hour_and_temperature():
    minutes = modetag.encode('idle', 5)
    out = modetag.apply(HALF, minutes)
    hh, rest = out.split()[1].split(':')
    mm, temp = rest.split('/')
    assert hh == '03', "hour must not move"
    assert temp == '20.5', "temperature must not change — the point is inert"
    assert int(mm) == minutes
    assert modetag.read(out) == minutes


def test_read_and_normalize():
    tagged = modetag.apply(SUN, modetag.encode('cooling', 2))
    assert modetag.read(tagged) == modetag.encode('cooling', 2)
    # normalize makes a tagged and an untagged schedule compare equal
    assert modetag.normalize(tagged) == modetag.normalize(SUN)


def test_schedule_comparison_ignores_the_tag():
    """Otherwise every tagged room reports a permanent settings_mismatch."""
    expected = {'schedule_sunday': SUN}
    reported = {'schedule_sunday': modetag.apply(SUN, modetag.encode('idle', 1))}
    assert common.compare_and_collect_mismatches(expected, reported) == {}


def test_schedule_comparison_still_catches_real_differences():
    expected = {'schedule_sunday': SUN}
    reported = {'schedule_sunday': SUN.replace('21.5', '19.0')}
    assert 'schedule_sunday' in common.compare_and_collect_mismatches(expected, reported)


def test_tag_payload_marks_only_carrier_days():
    payload = {f'schedule_{d}': SUN for d in
               ('monday', 'tuesday', 'wednesday', 'thursday', 'friday',
                'saturday', 'sunday')}
    modetag.tag_payload(payload, 'schedule', 'cooling', 1)
    for day in ('saturday', 'sunday'):
        assert modetag.read(payload[f'schedule_{day}']) == modetag.encode('cooling', 1)
    for day in ('monday', 'friday'):
        assert payload[f'schedule_{day}'] == SUN, "non-carrier days untouched"


def test_read_state_reports_disagreement():
    prefix = 'schedule'
    st = {f'{prefix}_saturday': modetag.apply(SUN, modetag.encode('cooling', 1)),
          f'{prefix}_sunday': modetag.apply(SUN, modetag.encode('idle', 1))}
    got = modetag.read_state(st, prefix)
    assert got['agree'] is False, "carrier days differing means someone rewrote it"


def test_read_state_agrees_when_both_match():
    prefix = 'weekly_schedule'          # TRVZB prefix
    tagged = modetag.apply(SUN, modetag.encode('heating', 3))
    st = {f'{prefix}_saturday': tagged, f'{prefix}_sunday': tagged}
    got = modetag.read_state(st, prefix)
    assert (got['mode'], got['generation'], got['agree']) == ('heating', 3, True)


def test_non_schedule_values_are_left_alone():
    assert not modetag.is_schedule('comfort')
    assert not modetag.is_schedule(None)
    assert modetag.apply('comfort', 5) == 'comfort'
    assert modetag.read('comfort') is None


# --- integration with build_intended_payload -------------------------------

TYPES = {
    'TR-M3Z': {'schedule_mode': {'system_mode': 'heat', 'preset': 'schedule'},
               'cooling_open': {'preset': 'comfort', 'comfort_temperature': 34},
               'manual_marker': {'field': 'preset', 'equals': 'manual'},
               'off_signature': {'system_mode': 'off', 'frost_protection': 'ON'}},
}
ITEM = {'day_hour': '05:00', 'day_temperature': 20.5, 'night_hour': '23:00',
        'night_temperature': 19.5, 'type': 'TR-M3Z'}
MQ = {'base_topic': 'zigbee2mqtt'}
OPEN = {'preset': 'comfort', 'comfort_temperature': 34, 'system_mode': 'heat'}


def _reported(extra, mode='cooling', gen=2):
    import cooling as _c
    base, _ = common.build_expected_payload('Waschküche', ITEM, TYPES, MQ)
    st = dict(extra)
    for d in modetag.CARRIER_DAYS:
        st[f'schedule_{d}'] = modetag.apply(base[f'schedule_{d}'],
                                            modetag.encode(mode, gen))
    return st


def _tag_of(payload):
    return modetag.read_state(payload, 'schedule')


def test_driving_modes_stamp_tag_and_bump_generation():
    import cooling
    rep = _reported(OPEN)
    for mode, want in (('cooling', 'cooling'), ('standby', 'idle'),
                       ('heating', 'heating')):
        p, _t, _n = cooling.build_intended_payload(
            'Waschküche', ITEM, TYPES, MQ, mode, rep, reclaim_manual=True)
        tag = _tag_of(p)
        assert (tag['mode'], tag['generation']) == (want, 3), mode


def test_manual_room_keeps_its_existing_tag():
    """Leaving a manual valve alone must not claim we put it in this season."""
    import cooling
    rep = _reported({'preset': 'manual'}, mode='cooling', gen=2)
    p, _t, note = cooling.build_intended_payload(
        'Waschküche', ITEM, TYPES, MQ, 'heating', rep)
    assert 'manual override' in note
    assert (_tag_of(p)['mode'], _tag_of(p)['generation']) == ('cooling', 2)


def test_off_room_is_tagged_idle_truthfully():
    """A closed valve is idle whatever the season wants. Without this nothing is
    ever tagged during standby, when every valve is off by design — and the tag
    rides in the schedule, so it forces nothing on."""
    import cooling
    rep = _reported({'system_mode': 'off', 'frost_protection': 'ON'},
                    mode='idle', gen=3)
    p, _t, note = cooling.build_intended_payload(
        'Waschküche', ITEM, TYPES, MQ, 'cooling', rep)
    assert 'off' in note
    assert _tag_of(p)['mode'] == 'idle'
    assert _tag_of(p)['generation'] == 0, "generation bumps from 3 -> 0 (wraps)"
    # and it must not have gained any control field
    assert 'preset' not in p and 'system_mode' not in p


def test_untagged_off_room_gets_tagged():
    import cooling
    base, _ = common.build_expected_payload('Waschküche', ITEM, TYPES, MQ)
    rep = {'system_mode': 'off', 'frost_protection': 'ON'}
    rep.update({f'schedule_{d}': base[f'schedule_{d}'] for d in modetag.CARRIER_DAYS})
    assert modetag.read_state(rep, 'schedule')['mode'] is None    # untagged
    p, _t, _n = cooling.build_intended_payload(
        'Waschküche', ITEM, TYPES, MQ, 'standby', rep)
    assert _tag_of(p)['mode'] == 'idle'


def test_generation_wraps():
    import cooling
    rep = _reported(OPEN, mode='cooling', gen=modetag.GENERATIONS - 1)
    p, _t, _n = cooling.build_intended_payload(
        'Waschküche', ITEM, TYPES, MQ, 'cooling', rep, reclaim_manual=True)
    assert _tag_of(p)['generation'] == 0
