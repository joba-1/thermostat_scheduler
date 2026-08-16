# Thermostat Scheduler & Monitor

A Python-based system for managing Zigbee thermostats via MQTT. Because manually configuring 10 thermostats every time you tweak the schedule is not anyone's idea of fun.

## What This Does

This project provides two complementary tools for controlling smart thermostats through zigbee2mqtt:

1. **`thermostat_scheduler.py`** — Pushes temperature schedules; checks and resets manual overrides
2. **`thermostat_monitor.py`** — The manager daemon: monitors device/sensor/heat-pump health, drives cooling, and emails low-noise alerts + a status overview

Together, they let you configure multiple thermostats from a single YAML file, keep rooms comfortable (warm in winter, cool in summer), and get told — sparingly — only when something actually needs you.

Full design and operations docs live in [`docs/`](docs/): [spec.md](docs/spec.md), [spec-manager.md](docs/spec-manager.md), [user.md](docs/user.md), [admin.md](docs/admin.md), [test.md](docs/test.md).

## Prerequisites

Before you start configuring thermostats, you'll need:

- **zigbee2mqtt** — Already set up and connected to your thermostats
- **MQTT broker** — Running and accessible (Mosquitto, etc.)
- **Python 3** — With `venv` support
- **Linux system** — For the monitor service (systemd-based)

If your thermostats aren't paired with zigbee2mqtt yet, go do that first. This tool assumes you've already survived that particular journey.

## Quick Start

1. **Edit** [`config.yaml`](config.yaml) with your MQTT broker details and thermostat settings
2. **Test** with a dry run to see what would be sent:
   ```bash
   python3 thermostat_scheduler.py --config config.yaml --dry-run
   ```
3. **Deploy** the schedule:
   ```bash
   python3 thermostat_scheduler.py --config config.yaml
   ```
4. **Verify** (if monitor service is running):
   ```bash
   python3 thermostat_scheduler.py --config config.yaml --check
   ```

## Configuration

All settings live in [`config.yaml`](config.yaml). The structure is straightforward:

### MQTT Settings
```yaml
mqtt:
  broker: 192.168.1.4
  port: 1883
  base_topic: zigbee2mqtt
  delay_between_messages: 5  # seconds between thermostat updates
  check_timeout: 1            # seconds to wait when checking device states
```

### Thermostat Types
Define how each thermostat brand expects its commands. Different manufacturers use different JSON keys because standards are apparently optional:

```yaml
thermostat_types:
  VNTH-T2_v2:
    schedule_mode:
      system_mode: heat
      preset: schedule
  TRVZB:
    schedule_mode:
      system_mode: auto
    schedule_prefix: weekly_schedule  # some use different key prefixes
```

### Individual Thermostats
```yaml
thermostats:
  Bedroom:
    day_hour: "06:00"
    day_temperature: 21.5
    night_hour: "23:00"
    night_temperature: 19.5
    type: VNTH-T2_v2
```

## The Scheduler Script

`thermostat_scheduler.py` is the main workhorse. It reads your config, generates schedules, and pushes them via MQTT.

### Usage Options

**Dry run** (see what would be published, don't actually send anything):
```bash
python3 thermostat_scheduler.py --config config.yaml --dry-run
```

**Normal run** (send schedules to thermostats):
```bash
python3 thermostat_scheduler.py --config config.yaml
```

**Check mode** (verify current device states match expected config):
```bash
python3 thermostat_scheduler.py --config config.yaml --check
```

Check mode requires the monitor service to be running. It queries the monitor for current device states and reports any mismatches.

### What It Does

For each thermostat, the scheduler:
1. Generates a daily schedule with 6 time points (3 day temps, 3 night temps)
2. Rounds times to 30-minute intervals
3. Ensures midnight (00:00) is always included
4. Applies the same schedule to all 7 days
5. Wraps it in the correct JSON structure for that thermostat type
6. Publishes to `{base_topic}/{Name} Thermostat/set`

See [SCHEDULER.md](SCHEDULER.md) for detailed installation and usage instructions.

## The Manager Daemon

`thermostat_monitor.py` runs continuously as a systemd service. It subscribes to
every thermostat, the configured zigbee2mqtt sensors, and the EMS-ESP heat pump
(all **directly over MQTT**, no Home Assistant), and on a timer evaluates health,
comfort and operating bounds.

### What it does

- **Health alerts** — battery low, lost device/sensor, thermostat not applying
  its settings, room not following its setpoint, water leak.
- **Cooling mode** — when the heat pump is cooling (or `season.mode: cooling`),
  forces every controllable valve fully open and restores the weekly schedule
  when heating resumes; rooms in manual override are left alone.
- **Sensors** — temperature vs setpoint (a window open explains a deviation
  instead of alerting), plus sensor battery/life-sign.
- **Low-noise email** — one mail per new issue, re-sent only after a cooldown,
  a daily digest, a periodic full status report, and a reminder to adjust your
  uncontrollable manual valves whenever the house switches heating↔cooling.
- **Status overview** — `thermostat_monitor.py --report [--mail]`.

It still answers `get` on the `thermostat_monitor` topic (per-device replies on
`thermostat_monitor/{Name}`) for the scheduler's `--check`.

See [MONITOR.md](MONITOR.md) for installation and [docs/](docs/) for details.

### Manual overrides

```bash
python3 thermostat_scheduler.py --list-manual            # which rooms are manual?
python3 thermostat_scheduler.py --reset-manual ["Name"]  # re-onboard into the season
```

Re-onboarding pushes the active season's state (cooling → open, heating →
schedule, standby → off). Without a `"Name"` it re-onboards exactly the rooms in
manual override, leaving controlled and switched-off rooms alone.

## Installation

### Automated Installation (Recommended)

The [`install.sh`](install.sh) script handles everything:

```bash
sudo ./install.sh [username] [repo_url]
```

This will:
- Create a system user (default: `thermostat`)
- Clone/copy the repository to `/home/thermostat/thermostat_scheduler`
- Set up a Python virtual environment
- Install dependencies from `requirements.txt`
- Create and enable the systemd service for the monitor

### Manual Installation

If automation makes you nervous, [MONITOR.md](MONITOR.md) has step-by-step manual instructions.

## Supported Thermostat Types

Currently tested with:
- **VNTH-T2_v2** — Various thermostats
- **TR-M3Z** — Tuya radiator valve
- **ME168_1** — Another variant
- **ME167** — Yet another one  
- **TRVZB** — Because why have one naming scheme when you can have five

Adding new types is straightforward: just define the required JSON keys in `thermostat_types` in your config.

## Key Features

- **Multi-device management** — Configure 10 thermostats as easily as 1
- **Type abstraction** — Handles different thermostat brands with different protocols
- **Schedule verification** — Check mode confirms your settings actually applied
- **Cooling mode** — Heat-pump-aware; opens valves for cooling, restores schedules for heating
- **Health & comfort monitoring** — Battery, lost devices/sensors, settings not applied, rooms off-target, leaks
- **Low-noise email alerts** — Throttled, with daily digest, status report and mode-change reminders
- **Manual-override aware** — Detect, respect, list and reset manual control
- **Dry-run mode** — Test configuration changes safely
- **Automated deployment** — One-command installation and service setup

## Project Structure

```
config.yaml / config.example.yaml  # Configuration + annotated example
thermostat_scheduler.py            # Schedule publisher / manual reset (on-demand)
thermostat_monitor.py              # Manager daemon (runs as service)
common.py                          # Shared helpers
heatpump.py cooling.py health.py sensors.py alerts.py   # Manager logic modules
tests/                             # pytest unit tests
docs/                              # spec / user / admin / test docs
install.sh                         # Automated installer
requirements.txt                   # Python dependencies
SCHEDULER.md / MONITOR.md          # Detailed scheduler / monitor docs
thermostat_monitor.service         # systemd service template
```

## Dependencies

- **paho-mqtt** ≥1.6.1 — MQTT client library
- **PyYAML** ≥6.0 — Config file parsing
- **pytest** ≥7.0 — Unit tests (dev)

Install with: `pip install -r requirements.txt`

## Contributing

If you have a different thermostat model that needs support, the easiest way is to:
1. Add a new entry to `thermostat_types` in your `config.yaml`
2. Figure out what JSON keys your device expects (check zigbee2mqtt logs)
3. Submit a pull request with the new type definition

## License

This is open-source software. Use it, modify it, break it, fix it. No warranty implied—if your house freezes because a thermostat didn't get the memo, that's between you and your heating bill.
