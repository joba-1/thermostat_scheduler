# Manager daemon — component spec

`thermostat_monitor.py` (`class Manager`) is the always-on process. It subscribes
to thermostat state, configured sensor topics and the EMS-ESP heat-pump topics,
and on a timer (`alerts.eval_interval`, default 300 s) runs one evaluation pass.

## Evaluation pass

1. `heatpump_state()` parses the latest `boiler_data` + `thermostat_data` into
   `{mode, cooling, active, telemetry}`.
2. `desired_mode(season, hp)` → `heating` | `cooling`.
3. Mode transition: on `heating<->cooling` change, `_notify_mode_change` mails a
   reminder to physically adjust `manual_thermostats` (skipped if none / first pass).
4. `collect_issues(mode, …)` (no side effects but history) gathers:
   - per thermostat: `health.classify_device` (life sign, battery, and — heating
     only — manual override vs settings mismatch); plus, when **not** manual,
     `sensors.evaluate_room` (temp vs setpoint, window-aware) and
     `health.no_reaction_issue` (valve demanding but room temp not following);
   - per sensor: `sensors.classify_sensor` (life sign, battery, leak);
   - heat pump: `heatpump.check_bounds` (only while `active`), as digest notes.
5. Cooling control (`_apply_cooling`): if `season.control`, publish each
   non-manual thermostat's `cooling_open` payload when entering cooling, or
   `cooling_restore` when entering heating. Per-device `applied_mode` avoids
   re-publishing; heating is seeded as the startup baseline without writing.
6. `Alerter.process(issues)` mails new / cooled-down alerts and records cleared
   ones; `maybe_send_digest()` sends the daily open-issues digest; `due()` gates
   the periodic full status report (`alerts.report_interval_hours`).

## Heat-pump remote sensor feed

The EMS-ESP heat pump expects a remote thermostat reading (room temperature +
humidity) which feeds its **dew-point protection** — the real lever that gates
cooling. The manager feeds this from a Zigbee room sensor (`heatpump.remote_feed`):

- On every (re)connect, `on_connect` publishes `control_value` (e.g. `RC100H`) to
  `control_topic` once, registering the remote thermostat type. Idempotent.
- A dedicated daemon thread (`publish_remote_feed`, every `interval` s, default
  60) republishes the source sensor's last `temperature`→`temp_topic` and
  `humidity`→`hum_topic`. The last known value is always sent so the pump never
  loses its remote reading.
- Freshness is a **safety** check, not cosmetic: `_remote_feed_issue` raises an
  **alert**-severity issue (mail + recovery mail via the `Alerter`) when the
  source sensor has not updated within `stale_after` s, or reports no humidity —
  because a stale/absent humidity makes the dew-point guard run blind.

Equivalent manual commands (for reference / one-off testing):

```
mosquitto_pub -t ems-esp/thermostat/hc1/control    -m RC100H   # once
mosquitto_pub -t ems-esp/thermostat/hc1/remotetemp -m 26.5     # every ~minute
mosquitto_pub -t ems-esp/thermostat/hc1/remotehum  -m 53       # every ~minute
```

## State

- `last_seen` / `last_state` (thermostats, keyed by room) and `sensor_seen` /
  `sensor_state` (keyed by friendly name) — separate namespaces.
- `history[room]`: ring buffer of `(ts, running_state, temp)` for no-reaction.
- `applied_mode[room]`: last cooling/heating action applied.
- `last_mode`: previous desired mode (transition detection).
- Alert/periodic state persisted by `Alerter` to `alerts.state_file` (JSON),
  surviving restarts so issues are not re-mailed.

## Setpoint resolution

`current_setpoint`: heating → the scheduled day/night temperature for the
current local time (day window = `[day_hour, night_hour)`); cooling →
`season.cool_target` (the comfort reference; valves are forced open regardless).

## CLI

- `--once` — one pass, then exit (cron-friendly; will drive cooling).
- `--report [--mail]` — connect, snapshot, print `status_report()`, optionally mail.
- (no flag) — run the evaluation loop forever.

While running, the daemon answers commands published to the `thermostat_monitor`
topic: `get` (per-device state replies), `report`/`status` (publish the full
`status_report()` text on `thermostat_monitor/_report`), and `status-mail` (also
email it). The scheduler's `--status` / `--status-mail` use these so the report
reflects the daemon's full live state. Alert mail uses the sender display name
from `alerts.from_name` (default: the running script name, e.g.
`thermostat_monitor`).

## Known limitations

- On AVATTO/SONOFF types the cooling-open state (`system_mode: heat`) is
  indistinguishable from a user manual `heat`; while the manager is driving
  cooling it treats its own action as non-manual (`applied_mode == 'cooling'`).
- No-reaction detection needs both a `running_state` and a temperature reading
  over the window; rooms without a temp sensor fall back to the thermostat's
  `local_temperature`.
