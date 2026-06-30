import health
import sensors as sensors_mod

TYPES = {
    'VNTH-T2_v2': {
        'schedule_mode': {'system_mode': 'heat', 'preset': 'schedule'},
        'manual_marker': {'field': 'preset', 'equals': 'manual'},
    },
}
ITEM = {'day_hour': '05:00', 'day_temperature': 21.5, 'night_hour': '23:00',
        'night_temperature': 19.5, 'type': 'VNTH-T2_v2'}
LIMITS = {'battery_limit': 20, 'unseen_interval': 1800}
NOW = 1_000_000


def kinds(issues):
    return {i.kind for i in issues}


def test_no_life_sign_when_never_seen():
    issues = health.classify_device('Bad OG', ITEM, TYPES, {'base_topic': 'z'},
                                    None, None, NOW, LIMITS)
    assert 'no_life_sign' in kinds(issues)


def test_battery_low_flag():
    reported = {'preset': 'schedule', 'system_mode': 'heat', 'battery_low': True}
    issues = health.classify_device('Bad OG', ITEM, TYPES, {'base_topic': 'z'},
                                    reported, NOW - 10, NOW, LIMITS)
    assert 'battery_low' in kinds(issues)


def test_manual_override_is_info_not_mismatch():
    reported = {'preset': 'manual', 'system_mode': 'heat'}
    issues = health.classify_device('Bad OG', ITEM, TYPES, {'base_topic': 'z'},
                                    reported, NOW - 10, NOW, LIMITS, mode='heating')
    assert 'manual_override' in kinds(issues)
    assert 'settings_mismatch' not in kinds(issues)
    assert [i for i in issues if i.kind == 'manual_override'][0].severity == 'info'


def test_cooling_mode_skips_mismatch():
    reported = {'preset': 'comfort', 'system_mode': 'heat'}
    issues = health.classify_device('Bad OG', ITEM, TYPES, {'base_topic': 'z'},
                                    reported, NOW - 10, NOW, LIMITS, mode='cooling')
    assert 'settings_mismatch' not in kinds(issues)
    assert 'manual_override' not in kinds(issues)


def test_no_reaction_detected():
    # 90 min demanding heat, temp barely moves, still below setpoint
    hist = [(NOW - 5400, 'heat', 18.0), (NOW - 2700, 'heat', 18.1), (NOW, 'heat', 18.2)]
    iss = health.no_reaction_issue('Bad OG', hist, 'heating', 21.0, 60)
    assert iss is not None and iss.kind == 'no_reaction'


def test_no_reaction_clears_when_warming():
    hist = [(NOW - 5400, 'heat', 18.0), (NOW, 'heat', 20.9)]
    assert health.no_reaction_issue('Bad OG', hist, 'heating', 21.0, 60) is None


def test_window_open_suppresses_temp_alert():
    windows = {'Bad OG': {'contact': False}}  # contact False == open
    issues = sensors_mod.evaluate_room('Bad OG', {'temperature': 17.0}, windows,
                                       21.0, 'heating', 1.5)
    assert kinds(issues) == {'window_open'}
    assert issues[0].severity == 'info'


def test_temp_alert_when_closed():
    windows = {'Bad OG': {'contact': True}}  # closed
    issues = sensors_mod.evaluate_room('Bad OG', {'temperature': 17.0}, windows,
                                       21.0, 'heating', 1.5)
    assert kinds(issues) == {'temp_unexpected'}


def test_improving_deviation_is_info_not_alert():
    # room above cooling target but trending toward it -> quiet note
    issues = sensors_mod.evaluate_room('Bad OG', {'temperature': 24.0}, {},
                                       21.0, 'cooling', 1.5, improving=True)
    assert kinds(issues) == {'temp_unexpected'}
    assert issues[0].severity == 'info'


def test_stuck_deviation_is_alert():
    issues = sensors_mod.evaluate_room('Bad OG', {'temperature': 24.0}, {},
                                       21.0, 'cooling', 1.5, improving=False)
    assert kinds(issues) == {'temp_unexpected'}
    assert issues[0].severity == 'alert'


def test_sensor_battery_and_life():
    issues = sensors_mod.classify_sensor('Bad OG Luft', 'temperature',
                                         {'battery': 5, 'temperature': 21.0},
                                         NOW - 10, NOW, {'battery_limit': 20})
    assert 'battery_low' in kinds(issues)


def test_stale_temp_issue_fires_past_threshold():
    iss = health.stale_temp_issue('Caros:tempstale', 'Caros thermostat',
                                  'TRV temperature', NOW - 5 * 3600, NOW, 4 * 3600)
    assert iss is not None
    assert iss.kind == 'stale_temperature' and iss.severity == 'alert'
    assert '5.0h' in iss.detail


def test_stale_temp_issue_quiet_within_threshold():
    assert health.stale_temp_issue('x', 'x', 'temperature',
                                   NOW - 3600, NOW, 4 * 3600) is None


def test_stale_temp_issue_disabled_with_zero():
    assert health.stale_temp_issue('x', 'x', 'temperature',
                                   NOW - 99 * 3600, NOW, 0) is None


def test_stale_temp_issue_none_when_no_timestamp():
    assert health.stale_temp_issue('x', 'x', 'temperature',
                                   None, NOW, 4 * 3600) is None
