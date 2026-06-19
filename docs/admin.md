# Admin / operations

## Installation

See `install.sh` (creates the service user, venv, installs requirements, and the
systemd unit). The daemon runs from `thermostat_monitor.service`.

```bash
sudo ./install.sh [username] [repo_url]
sudo systemctl enable --now thermostat_monitor.service
journalctl -u thermostat_monitor.service -f
```

## Configuration

All tunables live in `config.yaml`; `config.example.yaml` is the annotated
reference. Key sections: `mqtt`, `alerts`, `heatpump`, `season`, `sensors`,
`manual_thermostats`, `thermostat_types`, `thermostats`.

## Secrets (do NOT commit)

- **MQTT credentials**: prefer environment variables over `config.yaml`:
  `THERMOSTAT_MQTT_USER` / `THERMOSTAT_MQTT_PASS` (read by `common.mqtt_credentials`).
  For the service, put them in a mode-600 EnvironmentFile, e.g.
  `/etc/thermostat/secrets.env`, referenced from the unit (`EnvironmentFile=`).
- **Mail**: handled by the `send-mail` helper (`~/bin/send-mail.py` +
  `~/.config/sendmail-gmail.env`, mode 600). The repo never stores these.
- Where to find the values: password manager / Nextcloud (personal). The MQTT
  password is also in the broker/zigbee2mqtt config on the broker host.

## Logging

Both tools log via the `logging` module to stdout/stderr; the systemd unit
captures these into the journal with `SyslogIdentifier=thermostat_monitor`. Set
`THERMOSTAT_LOG_TS=1` for timestamps when running interactively.

## Backup & restore

- **Source**: private GitHub repo `joba-1/thermostat_scheduler` (push regularly).
- **Config**: committed (sanitised — no secrets).
- **Alert state** (`alerts.state_file`): transient; safe to lose (worst case a
  one-time re-alert). Not backed up.
- No database.

### Restore procedure

```
1. Install OS + Python3 + venv (see Installation)
2. git clone git@github.com:joba-1/thermostat_scheduler.git
3. Restore secrets: /etc/thermostat/secrets.env (MQTT) and
   ~/.config/sendmail-gmail.env (mail) from password manager / Nextcloud
4. python3 -m venv venv && venv/bin/pip install -r requirements.txt
5. sudo ./install.sh   (or just enable the systemd unit)
6. Verify: python3 thermostat_monitor.py --report
```

Test the restore on a spare host at least once after major changes.

## Troubleshooting

- **No device states in `--check`/`--report`**: the daemon must be running and
  subscribed; many TRVs only publish on change, so wait or nudge a setpoint.
- **False "battery unknown"**: mains/unknown-battery devices report no battery;
  these are not alerted (only `battery_low` / level < limit are).
- **Cooling not engaging**: confirm `coolingon` is `on` in
  `ems-esp/thermostat_data` (`mosquitto_sub -t 'ems-esp/#' -v`) and that
  `season.control` is true.
- **Per-type cooling payload wrong**: verify against one device of each type
  with `--dry-run` and zigbee2mqtt before trusting auto control (see test.md).
