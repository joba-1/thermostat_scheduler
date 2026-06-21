# Test plan

## Unit tests (`pytest`)

Run before every commit:

```bash
venv/bin/python -m pytest -q
```

Coverage (`tests/`):

| File | What it covers |
|------|----------------|
| `test_schedule.py` | schedule generation (midnight, 6 points), schedule/state comparison, `build_expected_payload` prefix |
| `test_heatpump_cooling.py` | EMS-ESP parse + `is_cooling`, `check_bounds`, `desired_mode`, manual-override detection, open/restore payloads |
| `test_health_sensors.py` | life sign / battery / manual vs mismatch / cooling-mode skip / no-reaction; window-open suppression; sensor battery+life |
| `test_alerts.py` | dedup + cooldown, info never mails, cleared removal, restart persistence, daily digest, periodic `due()` |
| `test_manager_report.py` | manual-overrides listing, sensor/thermostat namespace isolation, status report, mode-change notify |
| `test_devices.py` | device identity: ieee/name ref parsing, bridge/devices registry + rename detection, name-fallback resolution, HA entity candidates |
| `test_fan_control.py` | radiator-fan plug switching: cooling-active signal, on-debounce, off-delay hold through gaps, zigbee+tasmota topics, act:false |
| `test_history.py` | per-room charts: interval algebra, candidate (slug→ieee) resolution, SVG rendering |

Fixtures use the real captured `boiler_data` / `thermostat_data` field shapes.

## Component / integration (on minor releases)

1. **Dry run**: `thermostat_scheduler.py --dry-run` — payloads correct per type.
2. **Check loop**: start the daemon; `thermostat_scheduler.py --check` reports
   OK/mismatch against live state.
3. **Report**: `thermostat_monitor.py --report` shows live heat-pump telemetry
   and device/sensor states.
4. **Alert path**: run the daemon with a short `cooldown_hours`; simulate a low
   battery / unseen device and confirm exactly one mail + digest entry.

## Full system (on major releases / cooling changes)

1. **Per-type cooling payload**: for one device of each of the 5 types, apply
   `cooling_open` and confirm via zigbee2mqtt that the valve opens fully; apply
   `cooling_restore` and confirm the weekly schedule resumes unchanged.
   *(Open item: verify the AVATTO setpoint key and that the TECH/Tuya `comfort`
   preset tracks `comfort_temperature`.)*
2. **Mode flip**: force `season.mode: cooling` then `heating`; watch the daemon
   open all non-manual valves and then restore; confirm the manual-valve
   reminder mail fires on each transition.
3. **Manual respect**: put one room in manual; confirm cooling skips it and a
   note (not an alert) is raised.
