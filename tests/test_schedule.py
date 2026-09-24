from common import (
    generate_schedule_string,
    compare_schedule_strings,
    compare_and_collect_mismatches,
    build_expected_payload,
    dew_point,
)


def test_dew_point_basic_and_saturation():
    assert round(dew_point(25, 52), 1) == 14.5
    assert round(dew_point(20, 100), 1) == 20.0    # saturated -> dp == temp
    assert dew_point(25, 50) < 25                   # always below air temp


def test_dew_point_invalid_inputs():
    assert dew_point(None, 50) is None
    assert dew_point(25, None) is None
    assert dew_point(25, 0) is None                 # nonphysical RH
    assert dew_point(25, "bad") is None


def test_schedule_has_midnight_and_six_points():
    s = generate_schedule_string("05:00", 21.5, "23:00", 19.5)
    tokens = s.split()
    assert len(tokens) == 6
    assert tokens[0].startswith("00:00/")


def test_compare_schedule_ignores_insignificant_zeros():
    assert compare_schedule_strings("06:00/24.0", "06:00/24")
    assert not compare_schedule_strings("06:00/24.0", "06:00/23")


def test_mismatch_reports_missing_and_differing_keys():
    expected = {"system_mode": "auto", "preset": "schedule"}
    reported = {"system_mode": "heat"}
    mism = compare_and_collect_mismatches(expected, reported)
    assert "system_mode" in mism and "preset" in mism
    assert mism["preset"][1] is None


def test_build_expected_payload_uses_schedule_prefix():
    types = {"TRVZB": {"schedule_mode": {"system_mode": "auto"},
                       "schedule_prefix": "weekly_schedule"}}
    item = {"day_hour": "05:00", "day_temperature": 21, "night_hour": "23:00",
            "night_temperature": 19, "type": "TRVZB"}
    payload, topic = build_expected_payload("Caros", item, types, {"base_topic": "z"})
    assert "weekly_schedule_monday" in payload
    assert topic == "z/Caros Thermostat/set"


def test_trv_zbt_padding_is_not_a_mismatch():
    """The SONOFF TRV-ZBT reports 12 slots and pads a shorter schedule by
    repeating the last one (measured 2026-09-24). That must read as equal."""
    from common import schedules_equal, compare_and_collect_mismatches
    ours = "00:00/19.5 03:00/19.5 05:00/19.5 11:00/19.5 17:00/19.5 23:00/19.5"
    padded = ours + " 23:00/19.5" * 6
    assert schedules_equal(ours, padded)
    assert compare_and_collect_mismatches({"weekly_schedule_monday": ours},
                                          {"weekly_schedule_monday": padded}) == {}


def test_schedules_equal_ignores_tag_and_number_format():
    from common import schedules_equal
    import modetag
    ours = "00:00/19.5 03:00/19.0 05:00/21.0 11:00/21.0 17:00/21.0 23:00/19.5"
    tagged = modetag.apply(ours, 36)                      # carrier minute set
    device = tagged.replace("/19.0", "/19").replace("/21.0", "/21") + " 23:00/19.5" * 6
    assert schedules_equal(ours, device)


def test_schedules_equal_still_catches_real_differences():
    from common import schedules_equal
    ours = "00:00/19.5 03:00/19.5 05:00/21 11:00/21 17:00/21 23:00/19.5"
    assert not schedules_equal(ours, ours.replace("05:00/21", "06:00/21"))
    assert not schedules_equal(ours, ours.replace("05:00/21", "05:00/22"))
    assert not schedules_equal(ours, ours + " 23:30/16")   # an extra real slot
