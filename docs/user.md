# User documentation

## Everyday tasks

Push the schedules after editing `config.yaml`:

```bash
python3 thermostat_scheduler.py            # send schedules to all thermostats
python3 thermostat_scheduler.py --dry-run  # preview payloads, send nothing
python3 thermostat_scheduler.py --check    # compare live state vs config (needs the daemon)
```

### Manual overrides

If someone turns a thermostat to manual at the device, comfort control for that
room is suspended (and you get a low-priority note).

```bash
python3 thermostat_scheduler.py --list-manual           # which rooms are in manual?
python3 thermostat_scheduler.py --reset-manual           # clear all (re-push schedule)
python3 thermostat_scheduler.py --reset-manual "Bad OG"  # clear specific rooms
```

### Status overview

```bash
python3 thermostat_scheduler.py --status        # print a full overview
python3 thermostat_scheduler.py --status-mail   # ...and email it
```

These ask the **running daemon** for the report, so it is built from the
daemon's full accumulated state (real data, not the many `—` you'd get from a
cold one-shot). The equivalent one-shot still exists for when the daemon isn't
running: `python3 thermostat_monitor.py --report [--mail]`.

The overview shows the desired mode, heat-pump telemetry (with units), every
thermostat (state, setpoint, room temp, battery, last seen), every sensor, any
manual valves, and the open issues. The daemon also mails this automatically
every `report_interval_hours`. Mail arrives from the sender name
`thermostat_monitor`.

## Email alerts

The manager mails you (low-noise):

- **immediately** for each new alert (battery low, lost device/sensor, settings
  not applied, room not following setpoint, water leak) — re-sent only after
  `cooldown_hours` while it stays open;
- a **daily digest** at `digest_hour` of everything still open;
- a **periodic status report** (full overview);
- a **mode-change reminder** to open/reset your manual valves when the house
  switches between heating and cooling.

Window open while a room is cold/hot is reported as info, not an alert — it
explains the deviation.

## Cooling

When `season.mode: auto` and the heat pump reports cooling (`coolingon: on`),
the manager forces every controllable thermostat fully open (valves let cold
water through) and restores the weekly schedule when heating resumes. Rooms in
manual override are left alone. Set `season.mode: cooling`/`heating` to force a
mode regardless of the heat pump.
