#!/usr/bin/env python3
"""
Thermostat Manager (formerly "monitor") daemon.

One always-on process that:
  * subscribes to every thermostat state topic, every configured sensor topic,
    and the EMS-ESP heat-pump topics;
  * remembers last-seen time + last state, and answers `get` on the
    `thermostat_monitor` topic (kept for `thermostat_scheduler.py --check`);
  * on a timer, classifies thermostat/sensor health and room comfort, checks
    heat-pump bounds, and feeds issues to the throttled `Alerter` (mail +
    daily digest);
  * drives cooling mode: when the season is cooling (config or heat pump),
    forces every non-manual thermostat fully open, and restores the stored
    weekly schedule when heating resumes.

Uses the same `config.yaml` as `thermostat_scheduler.py`.
"""

import os
import html
import time
import json
import argparse
import threading
from collections import deque, defaultdict

import paho.mqtt.client as mqtt

from common import (setup_logging, log, load_config, time_to_minutes,
                    device_topic_name, mqtt_credentials)
import heatpump
import cooling
import health
import sensors as sensors_mod
from alerts import Alerter, make_issue

__version__ = "2.0.0"

DAY_MINUTES = 24 * 60


def iso_now():
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime())


def parse_iso(s):
    try:
        return time.mktime(time.strptime(s, '%Y-%m-%dT%H:%M:%S'))
    except Exception:
        return None


def current_setpoint(cfg_item, now_lt, mode, season_cfg):
    """Active target temperature for a room right now.

    Heating: scheduled day/night temperature for the current local time.
    Cooling: the configured cool target (we force valves open regardless, but
    this is the reference the comfort check compares the room temperature to).
    """
    if mode == 'cooling':
        return (season_cfg or {}).get('cool_target')
    cur = now_lt.tm_hour * 60 + now_lt.tm_min
    try:
        dh = time_to_minutes(cfg_item['day_hour'])
        nh = time_to_minutes(cfg_item['night_hour'])
    except Exception:
        return None
    day_span = (nh - dh) % DAY_MINUTES
    in_day = ((cur - dh) % DAY_MINUTES) < day_span
    return cfg_item['day_temperature'] if in_day else cfg_item['night_temperature']


class Manager:
    def __init__(self, cfg):
        self.cfg = cfg
        self.start_ts = time.time()
        self.mqtt_cfg = cfg.get('mqtt', {})
        self.thermostats = cfg.get('thermostats', {})
        self.thermostat_types = cfg.get('thermostat_types', {})
        self.alerts_cfg = cfg.get('alerts', {})
        self.heatpump_cfg = cfg.get('heatpump', {})
        self.season_cfg = cfg.get('season', {})
        self.sensors_cfg = cfg.get('sensors', {})
        self.manual_thermostats = cfg.get('manual_thermostats', []) or []
        self.last_mode = None        # for heating<->cooling transition detection
        self.base = self.mqtt_cfg.get('base_topic')
        self.monitor_topic = 'thermostat_monitor'

        self.alerter = Alerter(self.alerts_cfg)

        # Thermostats and sensors are separate namespaces: a contact sensor may
        # share a friendly name with a thermostat room (e.g. "Bad OG"), so they
        # must not share a state dict.
        self.last_seen = {}          # room -> iso ts  (thermostats)
        self.last_state = {}         # room -> payload (thermostats)
        self.thermo_topic = {}       # state topic -> room
        self.sensor_seen = {}        # friendly name -> iso ts
        self.sensor_state = {}       # friendly name -> payload
        self.sensor_topic = {}       # state topic -> friendly name
        self.sensor_kind = {}        # sensor name -> 'temperature'|'window'|'leak'
        self.room_temp_sensor = {}   # room -> temp sensor name
        self.room_windows = defaultdict(list)  # room -> [window sensor names]

        # heat-pump latest payloads
        self.hp_boiler = None
        self.hp_thermostat = None

        # room temperature/running-state history for no-reaction detection
        self.history = defaultdict(lambda: deque(maxlen=64))

        # cooling control bookkeeping: thermostat name -> last applied mode
        self.applied_mode = {}

        # Persisted device/sensor state so a restart starts with the last known
        # readings instead of a cold blank slate (no '?'/never in reports).
        self.device_state_file = os.path.expanduser(
            cfg.get('device_state_file',
                    '~/.local/state/thermostat_manager/devices.json'))

        self._build_topic_maps()
        self._load_device_state()

    def _build_topic_maps(self):
        for name, item in self.thermostats.items():
            topic = f"{self.base}/{device_topic_name(name)}"
            self.thermo_topic[topic] = name
            self.last_seen[name] = None
            self.last_state[name] = None
            sensors = item.get('sensors') or {}
            temp = sensors.get('temperature')
            if temp:
                self.room_temp_sensor[name] = temp
                self._register_sensor(temp, 'temperature')
            for w in sensors.get('windows') or []:
                self.room_windows[name].append(w)
                self._register_sensor(w, 'window')
            for leak in sensors.get('leak') or []:
                self._register_sensor(leak, 'leak')
        for extra in self.cfg.get('extra_sensors') or []:
            self._register_sensor(extra.get('name'), extra.get('kind', 'temperature'))

    def _register_sensor(self, name, kind):
        if not name or name in self.sensor_kind:
            return
        self.sensor_kind[name] = kind
        topic = f"{self.base}/{name}"
        self.sensor_topic[topic] = name
        self.sensor_seen[name] = None
        self.sensor_state[name] = None

    # ---- device-state persistence ------------------------------------
    def _load_device_state(self):
        """Restore last known device/sensor readings from disk (best-effort).

        Only keys still present in the current config are restored; unknown or
        removed devices are ignored. Staleness handling lives in
        `_effective_seen`, which gives every device a fresh grace window after
        startup so restoring an old timestamp never triggers a no-life-sign
        mail-storm on restart.
        """
        try:
            with open(self.device_state_file) as f:
                data = json.load(f)
        except Exception:
            return
        for room in self.last_state:
            if room in data.get('last_state', {}):
                self.last_state[room] = data['last_state'][room]
                self.last_seen[room] = data.get('last_seen', {}).get(room)
        for name in self.sensor_state:
            if name in data.get('sensor_state', {}):
                self.sensor_state[name] = data['sensor_state'][name]
                self.sensor_seen[name] = data.get('sensor_seen', {}).get(name)
        self.hp_boiler = data.get('hp_boiler', self.hp_boiler)
        self.hp_thermostat = data.get('hp_thermostat', self.hp_thermostat)
        log.info("restored device state from %s", self.device_state_file)

    def _save_device_state(self):
        """Persist current device/sensor readings (best-effort, atomic)."""
        data = {
            'last_seen': self.last_seen,
            'last_state': self.last_state,
            'sensor_seen': self.sensor_seen,
            'sensor_state': self.sensor_state,
            'hp_boiler': self.hp_boiler,
            'hp_thermostat': self.hp_thermostat,
        }
        try:
            os.makedirs(os.path.dirname(self.device_state_file), exist_ok=True)
            tmp = self.device_state_file + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self.device_state_file)
        except Exception as e:
            log.warning("could not persist device state: %s", e)

    # ---- MQTT --------------------------------------------------------
    def on_connect(self, client, userdata, flags, rc, properties=None):
        if rc != 0:
            log.error("MQTT connect failed with rc=%s", rc)
            return
        log.info("Connected to MQTT broker")
        for topic in self.thermo_topic:
            client.subscribe(topic)
        for topic in self.sensor_topic:
            client.subscribe(topic)
        client.subscribe(self.monitor_topic)
        if self.heatpump_cfg.get('enabled'):
            for t in (self.heatpump_cfg.get('boiler_topic'),
                      self.heatpump_cfg.get('thermostat_topic')):
                if t:
                    client.subscribe(t)
        log.info("Subscribed to %d thermostats + %d sensors + heat pump",
                 len(self.thermo_topic), len(self.sensor_topic))

    def on_message(self, client, userdata, msg):
        t = msg.topic
        payload = msg.payload.decode('utf-8', errors='ignore')

        if t == self.monitor_topic:
            cmd = payload.strip().lower()
            if cmd == 'get':
                self._respond_get(client)
            elif cmd in ('report', 'status', 'status-mail'):
                # Build from the daemon's full accumulated state so the report
                # has real data (a cold one-shot would show many '?').
                d = self._report_data()
                report = self._render_text(d)
                client.publish(f"{self.monitor_topic}/_report",
                               json.dumps({'report': report}), qos=1)
                if cmd == 'status-mail':
                    self.alerter.notify("[thermostat] status report", report,
                                        html_body=self._render_html(d))
                    log.info("status report mailed on request")
            return

        if t == self.heatpump_cfg.get('boiler_topic'):
            self.hp_boiler = self._safe_json(payload)
            return
        if t == self.heatpump_cfg.get('thermostat_topic'):
            self.hp_thermostat = self._safe_json(payload)
            return

        room = self.thermo_topic.get(t)
        if room:
            self.last_seen[room] = iso_now()
            self.last_state[room] = self._safe_json(payload)
            return
        sensor = self.sensor_topic.get(t)
        if sensor:
            self.sensor_seen[sensor] = iso_now()
            self.sensor_state[sensor] = self._safe_json(payload)

    @staticmethod
    def _safe_json(payload):
        try:
            return json.loads(payload)
        except Exception:
            return payload

    def _respond_get(self, client):
        for name in self.thermostats:
            resp = {
                'name': name,
                'last_seen': self.last_seen.get(name),
                'state': self.last_state.get(name),
            }
            client.publish(f"{self.monitor_topic}/{name}",
                           json.dumps(resp, indent=2, ensure_ascii=False), qos=1)

    # ---- evaluation --------------------------------------------------
    def heatpump_state(self):
        if not self.heatpump_cfg.get('enabled'):
            return None
        if self.hp_boiler is None and self.hp_thermostat is None:
            return None
        return heatpump.parse(self.hp_boiler, self.hp_thermostat, self.heatpump_cfg)

    def _limits(self):
        return {
            'battery_limit': self.alerts_cfg.get('battery_limit', 20),
            'unseen_interval': self.mqtt_cfg.get('unseen_interval', 1800),
        }, {
            'battery_limit': self.sensors_cfg.get('battery_limit', 20),
            'unseen_interval': self.sensors_cfg.get('unseen_interval', 7200),
        }

    def collect_issues(self, mode, now_ts, now_lt, hp):
        """Gather all issues without sending mail or driving devices.

        Pure-ish: only side effect is recording temperature history. Used by
        both the evaluation loop and the status report.
        """
        limits, sensor_limits = self._limits()
        issues = []

        # thermostats
        for name, item in self.thermostats.items():
            reported = self.last_state.get(name)
            seen_ts = self._effective_seen(self.last_seen.get(name))
            issues += health.classify_device(
                name, item, self.thermostat_types, self.mqtt_cfg,
                reported, seen_ts, now_ts, limits, mode=mode)

            type_cfg = self.thermostat_types.get(item.get('type'), {})
            manual = cooling.is_manual_override(type_cfg, reported)

            # comfort + no-reaction only when we're actually in control of the
            # room; a manually overridden room deviating is the user's choice.
            setpoint = current_setpoint(item, now_lt, mode, self.season_cfg)
            temp_state = self._room_temp_state(name)
            self._record_history(name, item, reported, temp_state, now_ts)
            if not manual:
                windows = {w: self.sensor_state.get(w) for w in self.room_windows.get(name, [])}
                improving = self._temp_improving(name, setpoint, mode)
                issues += sensors_mod.evaluate_room(
                    name, temp_state, windows, setpoint, mode,
                    item.get('tolerance', self.cfg.get('default_tolerance', 1.5)),
                    improving=improving)
                nr = health.no_reaction_issue(
                    name, list(self.history[name]), mode, setpoint,
                    item.get('no_reaction_minutes', self.cfg.get('default_no_reaction_minutes', 60)))
                if nr:
                    issues.append(nr)

        # standalone sensors (battery / life sign / leak)
        for name, kind in self.sensor_kind.items():
            seen_ts = self._effective_seen(self.sensor_seen.get(name))
            issues += sensors_mod.classify_sensor(
                name, kind, self.sensor_state.get(name), seen_ts, now_ts, sensor_limits)

        # heat-pump operating bounds (low-priority digest items, only while active)
        if hp and hp.get('active'):
            for field, val, low, high in heatpump.check_bounds(
                    hp['telemetry'], hp['mode'], self.heatpump_cfg.get('bounds', {})):
                issues.append(make_issue(
                    f"heatpump:{field}", 'hp_bounds', "heat pump",
                    f"{field}={val} outside [{low}, {high}] in {hp['mode']} mode",
                    severity='info'))
        return issues

    def evaluate(self, client=None):
        now_ts = time.time()
        now_lt = time.localtime(now_ts)
        hp = self.heatpump_state()
        mode = cooling.desired_mode(self.season_cfg, hp)
        issues = self.collect_issues(mode, now_ts, now_lt, hp)

        # heating<->cooling transition: remind operator about manual valves
        if self.last_mode is None:
            self.last_mode = mode           # baseline; no mail on first pass
        elif mode != self.last_mode:
            self._notify_mode_change(self.last_mode, mode)
            self.last_mode = mode

        # cooling control
        if client is not None and self.season_cfg.get('control', True):
            self._apply_cooling(client, mode, issues)

        mailed, cleared = self.alerter.process(issues)
        for iss in mailed:
            log.warning("ALERT %s: %s", iss.subject, iss.detail)
        for key in cleared:
            log.info("cleared: %s", key)
        self.alerter.maybe_send_digest()

        # periodic full status overview (not just problems)
        interval = self.alerts_cfg.get('report_interval_hours', 24) * 3600
        if interval > 0 and self.alerter.due('status_report', interval):
            d = self._report_data(mode, hp)
            self.alerter.notify("[thermostat] status report",
                                self._render_text(d), html_body=self._render_html(d))

        self._save_device_state()
        return issues

    def _effective_seen(self, iso_seen):
        """Last-seen timestamp for *alerting*, floored at the manager start time.

        A device gets a full unseen_interval grace period after every restart
        before we complain: never-seen falls back to start_ts, and a restored
        (possibly old) timestamp is clamped up to start_ts until the device
        reports again this session. The report's "seen" column uses the raw
        timestamp via `_age`, so it still shows the true age.
        """
        ts = parse_iso(iso_seen)
        if ts is None:
            return self.start_ts
        return max(ts, self.start_ts)

    def _room_temp_state(self, room):
        sensor = self.room_temp_sensor.get(room)
        if sensor and isinstance(self.sensor_state.get(sensor), dict):
            return self.sensor_state[sensor]
        # fall back to the thermostat's own local_temperature
        st = self.last_state.get(room)
        if isinstance(st, dict) and 'local_temperature' in st:
            return {'temperature': st.get('local_temperature')}
        return None

    def _temp_improving(self, name, setpoint, mode, epsilon=0.2):
        """True if the room temp is trending toward `setpoint`, or there isn't
        yet enough history to judge (e.g. just after a target/mode change).

        Keeps a fresh deviation as a quiet note instead of an immediate alert:
        only a deviation that is *stuck* (enough history, no progress) is loud.
        History samples are (ts, running_state, temp), oldest first.
        """
        if setpoint is None:
            return True
        hist = [(h[0], h[2]) for h in self.history[name]
                if isinstance(h[2], (int, float))]
        if len(hist) < 2:
            return True
        span = hist[-1][0] - hist[0][0]
        grace = self.cfg.get('comfort_grace_minutes', 30) * 60
        if span < grace:
            return True  # still settling after a recent change
        first, last = hist[0][1], hist[-1][1]
        if mode == 'cooling':
            return (first - last) > epsilon   # temperature falling
        return (last - first) > epsilon       # temperature rising

    def _record_history(self, name, item, reported, temp_state, now_ts):
        running = reported.get('running_state') if isinstance(reported, dict) else None
        temp = temp_state.get('temperature') if isinstance(temp_state, dict) else None
        self.history[name].append((now_ts, running, temp))

    def _notify_mode_change(self, old, new):
        """Mail a reminder to physically adjust uncontrollable manual valves."""
        log.info("mode change %s -> %s", old, new)
        if not self.manual_thermostats:
            return  # nothing actionable to remind about
        action = ("OPEN fully (for cooling)" if new == 'cooling'
                  else "set back to normal heating")
        lines = [f"House operating mode changed: {old} -> {new}.", "",
                 f"Please {action} these manual (non-controllable) thermostats:"]
        lines += [f"  - {loc}" for loc in self.manual_thermostats]
        log.warning("mode change %s -> %s; manual valves to %s: %s",
                    old, new, action, ", ".join(self.manual_thermostats))
        self.alerter.notify(f"[thermostat] mode changed to {new}", "\n".join(lines))

    def manual_overrides(self):
        """List rooms whose thermostat currently reports a manual override."""
        out = []
        for name, item in self.thermostats.items():
            type_cfg = self.thermostat_types.get(item.get('type'), {})
            if cooling.is_manual_override(type_cfg, self.last_state.get(name)):
                out.append(name)
        return out

    def _age(self, iso_seen):
        ts = parse_iso(iso_seen)
        if ts is None:
            return "never"
        mins = int((time.time() - ts) / 60)
        return f"{mins}m ago" if mins < 120 else f"{mins // 60}h ago"

    @staticmethod
    def _fmt(val, unit="", dash="—"):
        """Format a value with an optional unit, or a dash when missing."""
        if val is None or val == "" or val == "?":
            return dash
        if isinstance(val, float):
            val = f"{val:g}"
        return f"{val}{unit}"

    @staticmethod
    def _table(headers, rows, indent="  "):
        """Render aligned columns. Cells are strings; empty rows -> []."""
        if not rows:
            return []
        cols = list(zip(*([headers] + rows)))
        widths = [max(len(str(c)) for c in col) for col in cols]
        out = [indent + "  ".join(h.ljust(w) for h, w in zip(headers, widths)),
               indent + "  ".join("-" * w for w in widths)]
        for row in rows:
            out.append(indent + "  ".join(str(c).ljust(w)
                                          for c, w in zip(row, widths)))
        return out

    def _report_data(self, mode=None, hp=None):
        """Gather the status overview once as structured data for rendering."""
        now_ts = time.time()
        now_lt = time.localtime(now_ts)
        if hp is None:
            hp = self.heatpump_state()
        if mode is None:
            mode = cooling.desired_mode(self.season_cfg, hp)
        issues = self.collect_issues(mode, now_ts, now_lt, hp)
        n_alert = sum(1 for i in issues if i.severity == 'alert')
        n_info = sum(1 for i in issues if i.severity == 'info')
        overall = ("OK — nothing to report" if n_alert == 0 and n_info == 0
                   else f"{n_alert} alert(s), {n_info} note(s)")

        hp_line = None
        if hp:
            t = hp['telemetry']
            units = {'vorlauf': '°C', 'ruecklauf': '°C', 'outdoor': '°C',
                     'power': 'W', 'pressure': 'bar'}
            parts = [f"{k} {self._fmt(t[k], units.get(k, ''))}"
                     for k in ('vorlauf', 'ruecklauf', 'outdoor', 'power', 'pressure')
                     if k in t]
            act = 'running' if hp.get('active') else 'idle'
            hp_line = f"{hp['mode']} ({act}) — " + ", ".join(parts)

        manual_line = None
        if self.manual_thermostats:
            want = "OPEN fully" if mode == 'cooling' else "normal heating"
            manual_line = f"{want}: " + ", ".join(self.manual_thermostats)

        thermo_rows = []
        for name, item in self.thermostats.items():
            st = self.last_state.get(name) or {}
            type_cfg = self.thermostat_types.get(item.get('type'), {})
            manual = cooling.is_manual_override(type_cfg, st)
            sp = current_setpoint(item, now_lt, mode, self.season_cfg)
            temp_state = self._room_temp_state(name)
            temp = temp_state.get('temperature') if isinstance(temp_state, dict) else None
            state = (st.get('preset') or st.get('system_mode')
                     or st.get('running_state'))
            thermo_rows.append([
                name,
                ("MANUAL" if manual else self._fmt(state)),
                self._fmt(sp, "°C"),
                self._fmt(temp, "°C"),
                self._fmt(st.get('battery'), "%"),
                self._age(self.last_seen.get(name)),
            ])

        sensor_rows = None
        if self.sensor_kind:
            sensor_rows = []
            for name, kind in self.sensor_kind.items():
                st = self.sensor_state.get(name) or {}
                if not st:
                    val = "—"
                elif kind == 'window':
                    val = "OPEN" if sensors_mod.window_open(st) else "closed"
                elif kind == 'leak':
                    val = "LEAK" if st.get('water_leak') else "dry"
                else:
                    val = self._fmt(st.get('temperature'), "°C")
                sensor_rows.append([name, kind, val,
                                    self._fmt(st.get('battery'), "%"),
                                    self._age(self.sensor_seen.get(name))])

        order = {'alert': 0, 'info': 1}
        issue_list = [(i.severity, i.subject, i.detail)
                      for i in sorted(issues, key=lambda x: order.get(x.severity, 2))]

        return {
            'when': time.strftime('%a %Y-%m-%d %H:%M', now_lt),
            'overall': overall, 'mode': mode,
            'hp_line': hp_line, 'manual_line': manual_line,
            'thermo': {'headers': ["room", "state", "set", "temp", "bat", "seen"],
                       'rows': thermo_rows},
            'sensors': ({'headers': ["sensor", "kind", "value", "bat", "seen"],
                         'rows': sensor_rows} if sensor_rows is not None else None),
            'issues': issue_list,
        }

    @staticmethod
    def _render_text(d):
        bar = "=" * 56
        lines = [bar, "  Thermostat status report", f"  {d['when']}", bar,
                 f"  Overall : {d['overall']}", f"  Mode    : {d['mode']}"]
        if d['hp_line']:
            lines.append(f"  Heatpump: {d['hp_line']}")
        if d['manual_line']:
            lines.append(f"  Manual valves -> {d['manual_line']}")
        lines += ["", "  Thermostats"]
        lines += Manager._table(d['thermo']['headers'], d['thermo']['rows'])
        if d['sensors']:
            lines += ["", "  Sensors"]
            lines += Manager._table(d['sensors']['headers'], d['sensors']['rows'])
        if d['issues']:
            lines += ["", "  Open items"]
            for sev, subject, detail in d['issues']:
                tag = "ALERT" if sev == 'alert' else "note "
                lines.append(f"    [{tag}] {subject}: {detail}")
        lines.append(bar)
        return "\n".join(lines)

    @staticmethod
    def _html_table(headers, rows):
        """One scrollable HTML table (horizontal scroll on narrow screens)."""
        th = "".join(
            '<th style="text-align:left;padding:4px 12px 4px 0;'
            'border-bottom:2px solid #999;white-space:nowrap">'
            f'{html.escape(str(h))}</th>' for h in headers)
        trs = []
        for row in rows:
            tds = "".join(
                '<td style="padding:3px 12px 3px 0;border-bottom:1px solid #e2e2e2;'
                f'white-space:nowrap">{html.escape(str(c))}</td>' for c in row)
            trs.append(f"<tr>{tds}</tr>")
        return ('<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;'
                'margin:4px 0 16px">'
                '<table style="border-collapse:collapse;font-size:14px">'
                f'<thead><tr>{th}</tr></thead><tbody>{"".join(trs)}</tbody>'
                '</table></div>')

    @staticmethod
    def _render_html(d):
        esc = html.escape
        p = ['<div style="font-family:Arial,Helvetica,sans-serif;color:#222;'
             'max-width:100%">',
             '<h2 style="margin:0 0 2px;font-size:18px">Thermostat status</h2>',
             f'<div style="color:#777;font-size:13px;margin-bottom:10px">'
             f'{esc(d["when"])}</div>',
             '<table style="font-size:14px;margin-bottom:4px"><tbody>',
             f'<tr><td style="padding-right:12px;color:#777">Overall</td>'
             f'<td>{esc(d["overall"])}</td></tr>',
             f'<tr><td style="padding-right:12px;color:#777">Mode</td>'
             f'<td>{esc(d["mode"])}</td></tr>']
        if d['hp_line']:
            p.append('<tr><td style="padding-right:12px;color:#777;'
                     f'vertical-align:top">Heat pump</td><td>{esc(d["hp_line"])}</td></tr>')
        if d['manual_line']:
            p.append('<tr><td style="padding-right:12px;color:#777;'
                     f'vertical-align:top">Manual valves</td><td>{esc(d["manual_line"])}'
                     '</td></tr>')
        p.append('</tbody></table>')
        p.append('<h3 style="font-size:15px;margin:14px 0 2px">Thermostats</h3>')
        p.append(Manager._html_table(d['thermo']['headers'], d['thermo']['rows']))
        if d['sensors']:
            p.append('<h3 style="font-size:15px;margin:14px 0 2px">Sensors</h3>')
            p.append(Manager._html_table(d['sensors']['headers'], d['sensors']['rows']))
        if d['issues']:
            p.append('<h3 style="font-size:15px;margin:14px 0 2px">Open items</h3>')
            p.append('<ul style="margin:2px 0;padding-left:18px;font-size:14px">')
            for sev, subject, detail in d['issues']:
                color = '#b00020' if sev == 'alert' else '#777'
                tag = 'ALERT' if sev == 'alert' else 'note'
                p.append(f'<li style="margin:3px 0"><span style="color:{color};'
                         f'font-weight:bold">[{tag}]</span> {esc(subject)}: '
                         f'{esc(detail)}</li>')
            p.append('</ul>')
        p.append('</div>')
        return "".join(p)

    def status_report(self, mode=None, hp=None):
        """Plain-text overview (terminal / mail text part)."""
        return self._render_text(self._report_data(mode, hp))

    def status_report_html(self, mode=None, hp=None):
        """Mobile-friendly HTML overview (scrollable tables) for mail."""
        return self._render_html(self._report_data(mode, hp))

    def _apply_cooling(self, client, mode, issues):
        want = 'cooling' if mode == 'cooling' else 'heating'
        for name, item in self.thermostats.items():
            type_cfg = self.thermostat_types.get(item.get('type'), {})
            reported = self.last_state.get(name)
            # Respect user manual control, but don't mistake the cooling state
            # we ourselves applied (system_mode=heat on some types) for manual.
            if (self.applied_mode.get(name) != 'cooling'
                    and cooling.is_manual_override(type_cfg, reported)):
                continue  # user wins; already surfaced as info issue
            # seed heating baseline at startup without writing
            if name not in self.applied_mode and want == 'heating':
                self.applied_mode[name] = 'heating'
                continue
            if self.applied_mode.get(name) == want:
                continue
            payload = (cooling.build_open_payload(type_cfg) if want == 'cooling'
                       else cooling.build_restore_payload(type_cfg))
            if not payload:
                continue
            topic = f"{self.base}/{device_topic_name(name)}/set"
            try:
                client.publish(topic, json.dumps(payload), qos=1)
                self.applied_mode[name] = want
                log.info("cooling: set %s -> %s (%s)", name, want, payload)
            except Exception as e:
                log.error("cooling publish failed for %s: %s", name, e)


def main():
    parser = argparse.ArgumentParser(description='Thermostat manager daemon')
    parser.add_argument('--config', '-c', default='config.yaml', help='Path to YAML config file')
    parser.add_argument('--once', action='store_true', help='Run a single evaluation pass and exit')
    parser.add_argument('--report', action='store_true',
                        help='Connect, take a status snapshot, print it and exit')
    parser.add_argument('--mail', action='store_true',
                        help='With --report: also send the report by mail')
    args = parser.parse_args()

    setup_logging()
    cfg = load_config(args.config)
    mgr = Manager(cfg)

    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = mgr.on_connect
    client.on_message = mgr.on_message
    mqtt_user, mqtt_pass = mqtt_credentials(mgr.mqtt_cfg)
    if mqtt_user:
        client.username_pw_set(mqtt_user, mqtt_pass)

    log.info("Thermostat manager %s connecting to %s:%s",
             __version__, mgr.mqtt_cfg.get('broker'), mgr.mqtt_cfg.get('port'))
    client.connect(mgr.mqtt_cfg.get('broker'), mgr.mqtt_cfg.get('port'), keepalive=60)
    client.loop_start()

    eval_interval = mgr.alerts_cfg.get('eval_interval', 300)

    if args.report:
        # give the broker a moment to deliver retained/periodic device states
        time.sleep(max(mgr.mqtt_cfg.get('check_timeout', 5), 5))
        d = mgr._report_data()
        report = mgr._render_text(d)
        print(report)
        if args.mail:
            mgr.alerter.notify("[thermostat] status report", report,
                               html_body=mgr._render_html(d))
        client.loop_stop()
        client.disconnect()
        return

    if args.once:
        time.sleep(mgr.mqtt_cfg.get('check_timeout', 5))
        mgr.evaluate(client)
        client.loop_stop()
        client.disconnect()
        return

    # Flush device state on a systemd stop/restart (SIGTERM) too, not just Ctrl-C.
    import signal

    def _graceful(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _graceful)

    try:
        # let initial retained/periodic messages arrive before first pass
        time.sleep(min(eval_interval, 15))
        while True:
            try:
                mgr.evaluate(client)
            except Exception as e:
                log.exception("evaluation error: %s", e)
            time.sleep(eval_interval)
    except KeyboardInterrupt:
        mgr._save_device_state()
        client.loop_stop()
        client.disconnect()


if __name__ == '__main__':
    main()
