# Thermostat Scheduler & Manager — Specification

## Purpose

Keep every room comfortable with minimal operator attention: warm when it is
cold outside, cool when it is hot, while respecting end-user manual control and
alerting the operator (by email, sparingly) only when something needs a human —
a flat battery, a lost sensor, a thermostat ignoring its settings, or a room
not following its setpoint. The central EMS-ESP heat pump is the context signal
for whether the house should heat or cool and whether it operates within sane
boundaries.

## Components

| Component | File | Role |
|-----------|------|------|
| Scheduler | `thermostat_scheduler.py` | One-shot: push weekly schedules; `--check`, `--list-manual`, `--reset-manual` |
| Manager (daemon) | `thermostat_monitor.py` | Always-on: monitor health/comfort, drive cooling, alert, status report |
| Shared helpers | `common.py` | Config load, schedule gen, payload build, comparisons, logging, credentials |
| Heat pump | `heatpump.py` | Parse EMS-ESP MQTT → mode + telemetry; bounds check |
| Cooling | `cooling.py` | Desired season; per-type open/restore payloads; manual-override detection |
| Health | `health.py` | Classify thermostat issues (battery/life/mismatch/manual/no-reaction) |
| Sensors | `sensors.py` | Room comfort vs setpoint (window-aware); sensor battery/life |
| Alerts | `alerts.py` | Throttled mail, daily digest, periodic status report, JSON state |

The daemon name (`thermostat_monitor.py`) is retained from the original monitor
so the systemd unit and `--check` topic protocol stay stable; its role is now a
manager.

## Architecture & data flow

```
zigbee2mqtt/<Room> Thermostat ─┐
zigbee2mqtt/<Sensor> ──────────┤  MQTT   ┌───────────────┐
ems-esp/boiler_data ───────────┼────────►│  Manager      │── set ──► thermostats (cooling)
ems-esp/thermostat_data ───────┘         │  (daemon)     │── mail ─► operator (send-mail)
                                         └───────────────┘
thermostat_scheduler.py ── set ─────────► thermostats (schedules / reset-manual)
            │  get ◄── thermostat_monitor/<Room> ◄── Manager (--check, --list-manual)
```

All device, sensor and heat-pump reads are **direct from MQTT** — Home Assistant
is not involved, by design (fewer moving parts, no HA dependency).

### MQTT topic hierarchy

| Topic | Direction | Purpose |
|-------|-----------|---------|
| `zigbee2mqtt/<Room> Thermostat` | in | thermostat state |
| `zigbee2mqtt/<Room> Thermostat/set` | out | schedule / cooling / reset payloads |
| `zigbee2mqtt/<Sensor friendly name>` | in | temperature / contact / battery |
| `zigbee2mqtt/bridge/devices` | in | retained ieee ↔ friendly-name registry (device identity) |
| `ems-esp/boiler_data`, `ems-esp/thermostat_data` | in | heat-pump mode + telemetry |
| `thermostat_monitor` | in | `get` request |
| `thermostat_monitor/<Room>` | out | per-device state reply (for `--check`) |

## Design decisions (with reasoning)

- **Single daemon (manager), scheduler stays one-shot.** Health/comfort/cooling
  are continuous and stateful; the schedule push is a discrete operator action.
  Keeping the one-shot tool separate keeps each simple and testable.
- **Cooling via a high "open" setpoint, not an "eco" preset.** No universal eco
  preset exists across the five types, and eco means a *lower* temperature. A
  heating TRV opens its valve when the room is below setpoint; a high setpoint
  with cold water in the loop therefore cools the room. The weekly schedule
  remains stored on the device, so heating resumes with a `preset: schedule` /
  `system_mode: auto` restore — no reprogramming. Per-type payloads live in
  config (`cooling_open` / `cooling_restore`), mirroring `schedule_mode`.
- **`hpcooling: on` drives cooling**, chosen because it is stable across
  compressor cycles. `hpactivity` (live compressor state) toggles each cycle —
  used for the status report, not valve control — and `coolingon` was observed
  to stay `off` even while actively cooling; `hp4way` reads "cooling & defrost"
  from a resting valve even in winter. All configurable via `heatpump.cooling_when`.
- **Device identity = zigbee ieee, display = friendly name** (`devices.py`).
  Friendly names are mutable and differ between zigbee2mqtt and Home Assistant, so
  each device is anchored on its **ieee address** (the one stable id shared by both).
  Config gives each device an `ieee` + human `name`; the daemon caches z2m's retained
  `zigbee2mqtt/bridge/devices` registry on connect, resolves `ieee → current friendly
  name` to build the MQTT topic, and **re-subscribes if a device is renamed** in z2m
  (no restart). HA chart entities are resolved by **merging** the named slug and the
  `0x<ieee>_<property>` form, so history split across two entity ids by an HA rename
  stays continuous. Every step **falls back to the configured `name`**, so a
  missing/late registry degrades to plain friendly-name topics — identity-by-ieee is an
  added anchor, never a hard dependency for valve control. A bare-string device ref
  (legacy) still works (ieee looked up by name), but isn't rename-proof.
- **Thermostats and sensors are separate state namespaces.** A contact sensor
  may share a friendly name with a thermostat room (e.g. `Bad OG`); separate
  dicts (now ieee-anchored) prevent one clobbering the other.
- **Manual override is respected, not fought.** Detected via a per-type
  `manual_marker`; such rooms are skipped by cooling control and excluded from
  comfort/mismatch checks, and surfaced as a low-priority note.
- **Bounds checks only while the pump is active**, and never-seen devices get a
  full grace interval after startup — both avoid false alerts (idle summer flow
  temps; restart mail-storms).
- **Manual (uncontrollable) valves** are config-listed; the manager can't
  actuate them, so it emails an open/reset reminder on each mode transition.

See `spec-manager.md` for the daemon internals, `user.md` / `admin.md` for
operation, `test.md` for the test plan.
