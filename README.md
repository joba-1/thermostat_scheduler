# Thermostat Scheduler & Monitor

A Python-based system for managing Zigbee thermostats via MQTT. Because manually configuring 10 thermostats every time you tweak the schedule is not anyone's idea of fun.

## What This Does

This project provides two complementary tools for controlling smart thermostats through zigbee2mqtt:

1. **`thermostat_scheduler.py`** — Pushes temperature schedules to your thermostats
2. **`thermostat_monitor.py`** — Runs as a service, tracking device states and providing verification

Together, they let you configure multiple thermostats from a single YAML file and verify that your changes actually took effect (spoiler: sometimes they don't).

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

## The Monitor Service

`thermostat_monitor.py` runs continuously as a systemd service, subscribing to all thermostat state topics and remembering the last seen values.

### What It Monitors

- Last seen timestamp for each device
- Current state (JSON payload from zigbee2mqtt)
- Battery levels (reports low battery warnings)

### How It Works

The monitor subscribes to `{base_topic}/{Name} Thermostat` for each configured device. When you publish `get` to the `thermostat_monitor` topic, it responds with per-device status on `thermostat_monitor/{Name}`.

It also periodically publishes a list of devices that haven't reported in recently (default: 30 minutes) to `thermostat_monitor/unseen`.

See [MONITOR.md](MONITOR.md) for installation instructions.

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
- **Battery monitoring** — Get warnings when devices are running low
- **Dry-run mode** — Test configuration changes safely
- **Automated deployment** — One-command installation and service setup

## Project Structure

```
config.yaml                    # Main configuration file
thermostat_scheduler.py        # Schedule publisher (run on-demand)
thermostat_monitor.py          # State monitor (runs as service)
install.sh                     # Automated installer
requirements.txt               # Python dependencies
SCHEDULER.md                   # Detailed scheduler documentation
MONITOR.md                     # Detailed monitor documentation
thermostat_monitor.service     # systemd service template
```

## Dependencies

- **paho-mqtt** ≥1.6.1 — MQTT client library
- **PyYAML** ≥6.0 — Config file parsing

Install with: `pip install -r requirements.txt`

## Contributing

If you have a different thermostat model that needs support, the easiest way is to:
1. Add a new entry to `thermostat_types` in your `config.yaml`
2. Figure out what JSON keys your device expects (check zigbee2mqtt logs)
3. Submit a pull request with the new type definition

## License

This is open-source software. Use it, modify it, break it, fix it. No warranty implied—if your house freezes because a thermostat didn't get the memo, that's between you and your heating bill.
