Installing thermostat_scheduler

This file describes how to install and run `thermostat_scheduler.py`. You can reuse the virtual environment created for `thermostat_monitor.py` to avoid duplicating environments.

Paths used in examples assume the repository is at `/home/<user>/thermostat_scheduler` and the virtualenv is `/home/<user>/thermostat_scheduler/venv`.

1) Reuse existing venv (if created by the monitor installer)

```bash
/home/<user>/thermostat_scheduler/venv/bin/pip install -U pip
/home/<user>/thermostat_scheduler/venv/bin/pip install -r /home/<user>/thermostat_scheduler/requirements.txt
```

2) Dry-run to verify generated payloads (no MQTT publish)

```bash
/home/<user>/thermostat_scheduler/venv/bin/python \
  /home/<user>/thermostat_scheduler/thermostat_scheduler.py \
  --config /home/<user>/thermostat_scheduler/config.yaml --dry-run
```

3) Run to publish schedules to MQTT

```bash
/home/<user>/thermostat_scheduler/venv/bin/python \
  /home/<user>/thermostat_scheduler/thermostat_scheduler.py \
  --config /home/<user>/thermostat_scheduler/config.yaml
```

4) Create a new venv (if you don't want to reuse monitor's venv)

```bash
python3 -m venv /home/<user>/thermostat_scheduler/venv
/home/<user>/thermostat_scheduler/venv/bin/pip install -U pip
/home/<user>/thermostat_scheduler/venv/bin/pip install -r /home/<user>/thermostat_scheduler/requirements.txt
```

5) Automation

You can schedule `thermostat_scheduler.py` with cron or a systemd timer if you need periodic automated runs. If you reuse the monitor's venv, ensure the systemd unit or cron job uses the correct paths.
