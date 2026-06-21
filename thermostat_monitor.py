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
import urllib.parse
from collections import deque, defaultdict

import paho.mqtt.client as mqtt

from common import (setup_logging, log, load_config, time_to_minutes,
                    device_topic_name, mqtt_credentials, dew_point)
import heatpump
import cooling
import health
import history
import devices
import sensors as sensors_mod
from alerts import Alerter, make_issue

__version__ = "3.0.0"

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
        self.remote_feed_cfg = self.heatpump_cfg.get('remote_feed', {}) or {}
        self._remote_feed_selected = None     # sensor name currently feeding the pump
        self._remote_feed_last = None         # (temp, hum) last good value published
        self.manual_thermostats = cfg.get('manual_thermostats', []) or []
        self.last_mode = None        # for heating<->cooling transition detection
        self.base = self.mqtt_cfg.get('base_topic')
        self.monitor_topic = 'thermostat_monitor'

        self.alerter = Alerter(self.alerts_cfg)

        # Thermostats and sensors are separate namespaces: a contact sensor may
        # share a friendly name with a thermostat room (e.g. "Bad OG"), so they
        # must not share a state dict.
        # Devices are identified by their zigbee ieee (stable) and displayed by a
        # friendly label; see devices.py. Internally each device has a stable handle
        # (its config label / room); the volatile z2m friendly name is resolved from
        # the bridge/devices registry only to build the MQTT topic, and re-resolved on
        # rename. ieee is carried for chart lookups and rename detection.
        self.registry = devices.Registry()
        self.bridge_devices_topic = f"{self.base}/bridge/devices"

        self.last_seen = {}          # room -> iso ts  (thermostats)
        self.last_state = {}         # room -> payload (thermostats)
        self.thermo_topic = {}       # state topic -> room
        self.thermo_ieee = {}        # room -> TRV ieee
        self.thermo_sub_topic = {}   # room -> currently-subscribed state topic
        self.sensor_seen = {}        # label -> iso ts
        self.sensor_state = {}       # label -> payload
        self.sensor_topic = {}       # state topic -> label
        self.sensor_kind = {}        # label -> 'temperature'|'window'|'leak'
        self.sensor_ieee = {}        # label -> ieee
        self.sensor_friendly = {}    # label -> resolved friendly (topic) name
        self.sensor_sub_topic = {}   # label -> currently-subscribed state topic
        self.room_temp_sensor = {}   # room -> temp sensor label
        self.room_windows = defaultdict(list)  # room -> [window sensor labels]

        # heat-pump latest payloads
        self.hp_boiler = None
        self.hp_thermostat = None

        # room temperature/running-state history for no-reaction detection
        self.history = defaultdict(lambda: deque(maxlen=64))

        # cooling control bookkeeping: thermostat name -> last applied mode
        self.applied_mode = {}

        # window -> TRV control. window_off: rooms WE switched off because a
        # window is open (room -> iso ts), persisted so a restart keeps the latch
        # and only restores rooms we closed (never a user's manual off).
        self.window_cfg = cfg.get('window_control', {}) or {}

        # Radiator fans (smart plugs) switched on while the heat pump actively cools,
        # to boost radiator heat transfer. See _apply_fan_control.
        self.fan_cfg = cfg.get('fan_control', {}) or {}
        self.fans = self.fan_cfg.get('fans') or []
        self._fans_on = None        # last applied fan state (None until first apply)
        self._cool_active = None    # last seen "actively cooling" bool
        self._cool_edge_ts = 0.0    # when _cool_active last changed

        # Optional read-only status web page. The eval loop caches the latest
        # report data here; the HTTP server only renders this snapshot (it never
        # recomputes, so it has no side effects and can't race the MQTT thread).
        self.web_cfg = cfg.get('web', {}) or {}
        self._last_report = None
        # InfluxDB-backed per-room history charts (built lazily on first /room hit).
        self._history_cfg = self.web_cfg.get('history', {}) or {}
        self._influx = None

        self.window_off = {}
        self.window_rooms = defaultdict(list)   # window sensor name -> [rooms]
        self._win_timers = {}                   # room -> threading.Timer (debounce)
        self._win_lock = threading.Lock()

        # Persisted device/sensor state so a restart starts with the last known
        # readings instead of a cold blank slate (no '?'/never in reports).
        self.device_state_file = os.path.expanduser(
            cfg.get('device_state_file',
                    '~/.local/state/thermostat_manager/devices.json'))

        self._build_topic_maps()
        self._load_device_state()

    def _build_topic_maps(self):
        for name, item in self.thermostats.items():
            # The TRV: identity = its ieee; topic = resolved friendly name, falling
            # back to the "<room> Thermostat" convention until the registry arrives.
            trv_ref = {'ieee': item.get('ieee'), 'name': device_topic_name(name)}
            ieee, friendly, _ = devices.resolve(trv_ref, self.registry)
            topic = f"{self.base}/{friendly}"
            self.thermo_topic[topic] = name
            self.thermo_ieee[name] = ieee
            self.thermo_sub_topic[name] = topic
            self.last_seen[name] = None
            self.last_state[name] = None
            sensors = item.get('sensors') or {}
            temp = sensors.get('temperature')
            if temp:
                label = self._register_sensor(temp, 'temperature')
                self.room_temp_sensor[name] = label
            for w in sensors.get('windows') or []:
                label = self._register_sensor(w, 'window')
                self.room_windows[name].append(label)
                self.window_rooms[label].append(name)
            for leak in sensors.get('leak') or []:
                self._register_sensor(leak, 'leak')
        for extra in self.cfg.get('extra_sensors') or []:
            self._register_sensor(extra, extra.get('kind', 'temperature')
                                  if isinstance(extra, dict) else 'temperature')
        # Track the heat-pump remote-feed candidate sensors so the daemon
        # subscribes to them, shows them in the report, and can warn when stale.
        for cand in self._remote_feed_candidates():
            self._register_sensor(cand['ref'], 'temperature')

    def _trv_set_topic(self, room):
        """The /set topic for a room's TRV, using the registry-resolved friendly
        name (falls back to the '<room> Thermostat' convention)."""
        base = self.thermo_sub_topic.get(room) or \
            f"{self.base}/{device_topic_name(room)}"
        return f"{base}/set"

    def _register_sensor(self, ref, kind):
        """Register a device reference (string or {ieee,name}) as a monitored sensor.
        Returns the stable label/handle it is keyed under."""
        ieee, friendly, label = devices.resolve(ref, self.registry)
        if not label or label in self.sensor_kind:
            return label
        self.sensor_kind[label] = kind
        self.sensor_ieee[label] = ieee
        self.sensor_friendly[label] = friendly
        topic = f"{self.base}/{friendly}"
        self.sensor_topic[topic] = label
        self.sensor_sub_topic[label] = topic
        self.sensor_seen[label] = None
        self.sensor_state[label] = None
        return label

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
        # only keep window-off latches for rooms still in config
        self.window_off = {r: ts for r, ts in (data.get('window_off') or {}).items()
                           if r in self.thermostats}
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
            'window_off': self.window_off,
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
        # The z2m device registry (retained) maps ieee <-> friendly name; subscribe
        # first so it arrives before/with device state and topics resolve correctly.
        client.subscribe(self.bridge_devices_topic)
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
        # Re-assert the remote thermostat type on every (re)connect. Idempotent.
        rf = self.remote_feed_cfg
        if rf.get('enabled') and rf.get('control_topic') and rf.get('control_value'):
            client.publish(rf['control_topic'], str(rf['control_value']), qos=1)
            log.info("remote feed: sent %s=%s", rf['control_topic'], rf['control_value'])
        # The monitor is the sole window controller: turn off each TRV's own
        # window detection so the two don't fight. Idempotent, once per connect.
        if self.window_cfg.get('enabled') and self.window_cfg.get('disable_builtin', True):
            n = 0
            for name, item in self.thermostats.items():
                off = self.thermostat_types.get(item.get('type'), {}).get('builtin_window_off')
                if isinstance(off, dict) and off:
                    client.publish(self._trv_set_topic(name),
                                   json.dumps(off), qos=1)
                    n += 1
            if n:
                log.info("window-control: disabled built-in window detection on %d TRVs", n)

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

        if t == self.bridge_devices_topic:
            self._on_registry(self._safe_json(payload), client)
            return

        if t == self.heatpump_cfg.get('boiler_topic'):
            self.hp_boiler = self._safe_json(payload)
            self._apply_fan_control(client)   # react promptly to cooling on/off
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
            if self.sensor_kind.get(sensor) == 'window':
                self._on_window_event(sensor, client)

    def _on_registry(self, payload, client):
        """Handle a z2m bridge/devices update: refresh the ieee<->name registry and
        re-point any device whose friendly name changed to its new topic. Keeps the
        daemon tracking a device across a z2m rename without a restart."""
        if not isinstance(payload, list):
            return
        self.registry.update(payload)
        # Back-fill ieee for any device configured by name only (no ieee in config):
        # the maps were built before the registry arrived, so resolve name -> ieee now.
        for room in self.thermo_ieee:
            if self.thermo_ieee[room] is None:
                self.thermo_ieee[room] = self.registry.ieee_of(self._trv_friendly(room))
        for label in self.sensor_ieee:
            if self.sensor_ieee[label] is None:
                self.sensor_ieee[label] = self.registry.ieee_of(
                    self.sensor_friendly.get(label) or label)
        moved = 0
        # thermostats (keyed by room, identity = TRV ieee)
        for room, ieee in self.thermo_ieee.items():
            friendly = self.registry.name_of(ieee)
            if not friendly:
                continue
            new_topic = f"{self.base}/{friendly}"
            old_topic = self.thermo_sub_topic.get(room)
            if new_topic != old_topic:
                moved += self._resub(client, old_topic, new_topic, self.thermo_topic, room)
                self.thermo_sub_topic[room] = new_topic
        # sensors (keyed by label, identity = sensor ieee)
        for label, ieee in self.sensor_ieee.items():
            friendly = self.registry.name_of(ieee) if ieee else None
            if not friendly or friendly == self.sensor_friendly.get(label):
                continue
            new_topic = f"{self.base}/{friendly}"
            old_topic = self.sensor_sub_topic.get(label)
            moved += self._resub(client, old_topic, new_topic, self.sensor_topic, label)
            self.sensor_friendly[label] = friendly
            self.sensor_sub_topic[label] = new_topic
        if moved:
            log.info("registry: %d device(s) re-subscribed after a rename", moved)

    @staticmethod
    def _resub(client, old_topic, new_topic, topic_map, handle):
        """Move a subscription old_topic -> new_topic and update topic_map."""
        if old_topic == new_topic:
            return 0
        if old_topic in topic_map:
            del topic_map[old_topic]
            try:
                client.unsubscribe(old_topic)
            except Exception:
                pass
        topic_map[new_topic] = handle
        try:
            client.subscribe(new_topic)
        except Exception:
            pass
        return 1

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

    # ---- heat-pump remote sensor feed --------------------------------
    def _remote_feed_candidates(self):
        """Normalized list of {ref, sensor, room} candidates. `ref` is the device
        reference (string or {ieee,name}); `sensor` is its resolved stable label
        (the key into sensor_state). Supports schema v2 `{ieee,name,room}`, the
        legacy `{sensor,room}` and bare-string forms, and a single `sensor:`."""
        rf = self.remote_feed_cfg
        if not rf.get('enabled'):
            return []
        out = []
        for s in rf.get('sensors') or []:
            if isinstance(s, dict):
                ref = s if ('ieee' in s or 'name' in s) else s.get('sensor')
                room = s.get('room')
            else:
                ref, room = s, None
            if ref is None:
                continue
            _, _, label = devices.resolve(ref, self.registry)
            out.append({'ref': ref, 'sensor': label, 'room': room})
        if not out and rf.get('sensor'):       # backward-compatible single sensor
            _, _, label = devices.resolve(rf['sensor'], self.registry)
            out.append({'ref': rf['sensor'], 'sensor': label, 'room': None})
        return out

    def _remote_feed_select(self, now_ts):
        """Pick the warmest fresh candidate to feed the pump's dew-point guard.

        Among candidates with both temp+humidity that aren't completely stale,
        prefer the warmest **window-closed** room (most representative of the
        sealed-house dew load). If none of those are fresh, fall back to the
        warmest **window-open** room: a current reading, even with a window open,
        is better dew-point data than a stale last-good value. Only when nothing
        fresh remains does this return None (caller then holds the last value and
        alerts). The result carries `window_open` so the caller can flag the
        degraded (open-window) feed.
        """
        stale_after = self.remote_feed_cfg.get('stale_after', 1800)
        best_closed = best_fresh = None
        for cand in self._remote_feed_candidates():
            st = self.sensor_state.get(cand['sensor'])
            if not isinstance(st, dict):
                continue
            temp, hum = st.get('temperature'), st.get('humidity')
            if temp is None or hum is None:        # dew point needs both, same sensor
                continue
            # Use the *real* last-seen here, not the alert grace (`_effective_seen`):
            # the dew-point guard must run on genuinely recent data, never on a
            # 12 h-old value that only looks fresh because we just restarted.
            seen = parse_iso(self.sensor_seen.get(cand['sensor']))
            if seen is None or now_ts - seen > stale_after:
                continue                            # no real recent reading -> never use
            win = bool(cand.get('room') and self._room_window_open(cand['room']))
            entry = {'sensor': cand['sensor'], 'room': cand.get('room'),
                     'temp': temp, 'hum': hum, 'window_open': win}
            if best_fresh is None or temp > best_fresh['temp']:
                best_fresh = entry
            if not win and (best_closed is None or temp > best_closed['temp']):
                best_closed = entry
        return best_closed or best_fresh

    def publish_remote_feed(self, client):
        """Publish the warmest eligible room's temp+humidity to the heat pump.

        temp and humidity always come from the same sensor (coherent dew point).
        If no candidate qualifies we republish the last good value so the pump
        never loses its reading; the staleness is surfaced as an alert.

        `temp_offset` (K, default 0) is added to the fed temperature before
        publishing. The dew/comfort sensors sit high in the room while the
        radiators (and the coldest surfaces) are lower and cooler, so a negative
        offset feeds a temperature closer to the floor-level air. Because the
        pump derives its dew-point floor from the fed temp+humidity, a lower fed
        temp lowers `dewtemperature`, allowing a colder flow (down to the hard
        `hpminflowtemp` floor).
        """
        rf = self.remote_feed_cfg
        if not rf.get('enabled') or client is None:
            return
        sel = self._remote_feed_select(time.time())
        if sel is None:
            if self._remote_feed_last is None:
                return
            temp, hum = self._remote_feed_last      # keep the pump fed with last good
        else:
            if sel['sensor'] != self._remote_feed_selected:
                degraded = " [window open - no fresh closed room]" if sel.get('window_open') else ""
                log.info("remote feed: source -> %s (%s, %.1f°C %s%%RH)%s",
                         sel['sensor'], sel.get('room') or '?', sel['temp'], sel['hum'],
                         degraded)
                self._remote_feed_selected = sel['sensor']
            temp, hum = sel['temp'], sel['hum']
            self._remote_feed_last = (temp, hum)
        offset = rf.get('temp_offset', 0) or 0
        fed_temp = round(temp + offset, 1) if temp is not None else None
        if fed_temp is not None and rf.get('temp_topic'):
            client.publish(rf['temp_topic'], str(fed_temp), qos=1)
        if hum is not None and rf.get('hum_topic'):
            client.publish(rf['hum_topic'], str(hum), qos=1)

    def _remote_feed_issue(self, now_ts):
        """Alert when NO candidate can feed a fresh, coherent temp+humidity (every
        candidate is completely stale or reports no humidity — a fresh window-open
        room is still used, so an open window alone no longer triggers this).
        Dew-point protection then runs on a stale value, so this is alert-severity,
        not a quiet note."""
        if not self._remote_feed_candidates():
            return None
        if self._remote_feed_select(now_ts) is not None:
            return None
        return make_issue(
            'remotefeed:stale', 'remote_feed_stale', 'heat-pump remote feed',
            "no fresh room temp+humidity to feed the pump (every candidate is "
            "completely stale or reports no humidity); dew-point protection is "
            "running on a stale value")

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
            # In cooling, flag a non-manual thermostat that isn't actually fully
            # open. A room switched *off* (e.g. window open) is intended, not a
            # drift, so it is excluded. Note: a Tuya/battery TRV takes several
            # seconds to apply a /set and report back, so a freshly-commanded
            # valve can read its old state briefly — this flag may lag reality
            # by a report cycle. Report only; we don't fight it (season.control).
            if (mode == 'cooling' and not manual and isinstance(reported, dict)
                    and not cooling.is_off(reported)
                    and not cooling.is_open(type_cfg, reported)):
                issues.append(make_issue(
                    f"{name}:notopen", 'cooling_not_open', f"{name} thermostat",
                    "not fully open in cooling mode (drifted from the open setpoint)",
                    severity='info'))

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

        # heat-pump remote sensor feed freshness (dew-point safety -> alert)
        rf_issue = self._remote_feed_issue(now_ts)
        if rf_issue:
            issues.append(rf_issue)

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

        # window control is event-driven, but reconcile every pass too so a room
        # left in a stale state (e.g. after a restart, or a sleepy contact that
        # rarely reports) is corrected within an eval interval rather than waiting
        # for the next window event.
        if client is not None and self.window_cfg.get('enabled'):
            for room in self.thermostats:
                try:
                    self._apply_window_control(room, client)
                except Exception as e:
                    log.exception("window-control reconcile error for %s: %s", room, e)

        # radiator fans: also here so the off_delay still expires if boiler
        # messages pause (it's normally driven by each boiler_data update).
        self._apply_fan_control(client, hp)

        mailed, cleared = self.alerter.process(issues)
        for iss in mailed:
            log.warning("ALERT %s: %s", iss.subject, iss.detail)
        for key in cleared:
            log.info("cleared: %s", key)
        self.alerter.maybe_send_digest()

        # Cache the overview for the status web page (rendered on demand from
        # this snapshot, so the HTTP thread never recomputes / records history).
        d = self._report_data(mode, hp, issues=issues)
        self._last_report = d

        # periodic full status overview (not just problems)
        interval = self.alerts_cfg.get('report_interval_hours', 24) * 3600
        if interval > 0 and self.alerter.due('status_report', interval):
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

    # ---- window -> TRV control ---------------------------------------
    def _room_window_open(self, room):
        return any(sensors_mod.window_open(self.sensor_state.get(w))
                   for w in self.room_windows.get(room, []))

    def _window_debounce(self, room, opening):
        wc = self.thermostats.get(room, {}).get('window') or {}
        if 'debounce' in wc:
            return wc['debounce']
        return self.window_cfg.get('open_debounce' if opening else 'close_debounce', 5)

    def _humidity_guard_ok(self, room):
        """Rooms with a humidity_guard only ventilate (switch off) when dry
        enough. If the guard sensor has no reading, ventilation is skipped — the
        room keeps heating (matches the HA laundry-room behaviour)."""
        guard = (self.thermostats.get(room, {}).get('window') or {}).get('humidity_guard')
        if not isinstance(guard, dict):
            return True
        st = self.sensor_state.get(guard.get('sensor'))
        hum = st.get('humidity') if isinstance(st, dict) else None
        if hum is None:
            return False
        return hum < guard.get('below', 50)

    def _on_window_event(self, sensor, client):
        """A contact sensor changed: (re)arm the debounce timer for its room(s)."""
        if not self.window_cfg.get('enabled'):
            return
        for room in self.window_rooms.get(sensor, []):
            opening = self._room_window_open(room)
            delay = self._window_debounce(room, opening)
            with self._win_lock:
                old = self._win_timers.pop(room, None)
                if old:
                    old.cancel()
                timer = threading.Timer(delay, self._apply_window_control,
                                        args=(room, client))
                timer.daemon = True
                self._win_timers[room] = timer
                timer.start()

    @staticmethod
    def _cooling_active(hp):
        """True when the heat pump is actively producing cold (compressor cooling) —
        i.e. the radiators have cold water worth fanning. Uses `hpactivity` (the live
        compressor activity), the same authoritative signal the charts use."""
        return bool(hp) and (hp.get('raw') or {}).get('hpactivity') == 'cooling'

    def _set_fan(self, client, fan, on, dry=False):
        """Switch one fan plug. Supports zigbee2mqtt and Tasmota plugs."""
        ftype = (fan.get('type') or '').lower()
        state = "ON" if on else "OFF"
        if ftype == 'zigbee':
            name = fan.get('name')
            if not name:
                return
            topic, payload = f"{self.base}/{name}/set", json.dumps({"state": state})
        elif ftype == 'tasmota':
            dev = fan.get('topic') or fan.get('name')
            if not dev:
                return
            topic = f"{fan.get('cmnd_prefix', 'cmnd')}/{dev}/{fan.get('power', 'POWER')}"
            payload = state
        else:
            log.warning("fan-control: unknown fan type %r", fan.get('type'))
            return
        if dry:
            log.info("fan-control: would publish %s = %s", topic, payload)
            return
        client.publish(topic, payload, qos=1)

    def _apply_fan_control(self, client, hp=None, now=None):
        """Drive the radiator-fan plugs from the cooling signal: ON while the heat
        pump actively cools (after `on_debounce`), held ON for `off_delay` after it
        stops so the fans keep working the buffer's residual cold through the
        compressor's off-gaps. Idempotent — only publishes on a state change."""
        if client is None or not (self.fan_cfg.get('enabled') and self.fans):
            return
        now = now if now is not None else time.time()
        if hp is None:
            hp = self.heatpump_state()
        active = self._cooling_active(hp)
        if active != self._cool_active:
            self._cool_active = active
            self._cool_edge_ts = now
        if active:
            want = (now - self._cool_edge_ts) >= self.fan_cfg.get('on_debounce', 30)
            want = want or bool(self._fans_on)        # already on -> stay on
        else:
            want = bool(self._fans_on) and \
                (now - self._cool_edge_ts) < self.fan_cfg.get('off_delay', 600)
        if want == bool(self._fans_on):     # None treated as off; no spurious publish
            self._fans_on = want
            return
        act = self.fan_cfg.get('act', True)
        for fan in self.fans:
            try:
                self._set_fan(client, fan, want, dry=not act)
            except Exception as e:
                log.error("fan-control publish failed for %s: %s", fan, e)
        self._fans_on = want
        log.info("fan-control: cooling=%s -> fans %s%s", active,
                 "ON" if want else "OFF", "" if act else " (act:false)")

    def _apply_window_control(self, room, client):
        """Fired after the debounce: switch the room's TRV off (window open) or
        restore its intended state (window closed), honouring manual override and
        the 'only restore rooms we closed' latch."""
        with self._win_lock:
            self._win_timers.pop(room, None)
        if not self.window_cfg.get('enabled'):
            return
        act = self.window_cfg.get('act', True)
        item = self.thermostats.get(room)
        if not item:
            return
        type_cfg = self.thermostat_types.get(item.get('type'), {})
        reported = self.last_state.get(room)
        any_open = self._room_window_open(room)

        # Do we own this room's off? Either the device shows our off_signature, or
        # we have it latched and it is still off (covers a plain off we set with an
        # older version, before it's upgraded to the signature).
        own_off = (cooling.is_our_off(type_cfg, reported)
                   or (room in self.window_off and cooling.is_off(reported)))

        # A user/other controller took manual control (and it isn't our off) ->
        # leave it alone and stop tracking it.
        if cooling.is_manual_override(type_cfg, reported) and not own_off:
            log.info("window-control %s: window=%s — skip (manual override)",
                     room, "open" if any_open else "closed")
            if self.window_off.pop(room, None) is not None:
                self._save_device_state()
            return

        topic = self._trv_set_topic(room)
        if any_open:
            if own_off:
                return  # already off and ours (signature or latched)
            if not self._humidity_guard_ok(room):
                log.info("window-control %s: window open but humidity guard blocks "
                         "ventilation — leaving heating on", room)
                return
            # "our off" = the type's off_signature (e.g. off + frost_protection ON),
            # distinct from a user's plain off so we only auto-restore what we set.
            off_payload = type_cfg.get('off_signature') or {'system_mode': 'off'}
            log.info("window-control %s: window OPEN -> off %s%s",
                     room, off_payload, "" if act else " (act=false, not sent)")
            if act and client is not None:
                client.publish(topic, json.dumps(off_payload), qos=1)
            self.window_off[room] = iso_now()
            self._save_device_state()
        else:
            # Restore only what WE switched off.
            if not own_off:
                if room in self.window_off:
                    log.info("window-control %s: window closed but state isn't our "
                             "off — leaving it (user took over)", room)
                    self.window_off.pop(room, None)
                    self._save_device_state()
                return
            mode = cooling.desired_mode(self.season_cfg, self.heatpump_state())
            payload, _, _ = cooling.build_intended_payload(
                room, item, self.thermostat_types, self.mqtt_cfg, mode, None)
            if mode == 'cooling':
                payload.setdefault('system_mode', 'heat')  # ensure the valve turns on
            payload.update(type_cfg.get('off_clear') or {})  # undo the off marker
            log.info("window-control %s: window CLOSED -> restore (%s)%s",
                     room, mode, "" if act else " (act=false, not sent)")
            if act and client is not None:
                client.publish(topic, json.dumps(payload), qos=1)
            self.window_off.pop(room, None)
            self._save_device_state()

    def _temp_improving(self, name, setpoint, mode):
        """True if the room is closing the gap to `setpoint` fast enough, or
        there isn't yet enough history to judge (e.g. just after a change).

        Keeps a deviation as a quiet note while it is being worked off at the
        expected rate; once progress falls below that rate (stalled *or* merely
        crawling toward target) it escalates to a loud alert. The expected rate
        is 1 °C per `comfort_minutes_per_degree` minutes (default 60 -> 1 °C/h),
        so a deviation that won't clear in ~1 h per °C gets surfaced.
        History samples are (ts, running_state, temp), oldest first.
        """
        if setpoint is None:
            return True
        hist = [(h[0], h[2]) for h in self.history[name]
                if isinstance(h[2], (int, float))]
        if len(hist) < 2:
            return True
        span_min = (hist[-1][0] - hist[0][0]) / 60.0
        grace = self.cfg.get('comfort_grace_minutes', 30)
        if span_min < grace:
            return True  # still settling after a recent change
        first, last = hist[0][1], hist[-1][1]
        progress = (first - last) if mode == 'cooling' else (last - first)
        rate = progress / span_min            # °C/min toward target
        required = 1.0 / self.cfg.get('comfort_minutes_per_degree', 60)
        return rate >= required

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
        intro = (f"House operating mode changed: {old} -> {new}. "
                 f"Please {action} these manual (non-controllable) thermostats:")
        lines = [intro] + [f"  - {loc}" for loc in self.manual_thermostats]
        log.warning("mode change %s -> %s; manual valves to %s: %s",
                    old, new, action, ", ".join(self.manual_thermostats))
        self.alerter.notify(f"[thermostat] mode changed to {new}", "\n".join(lines),
                            html_body=Alerter.html_message(intro, self.manual_thermostats))

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

    def _report_data(self, mode=None, hp=None, issues=None):
        """Gather the status overview once as structured data for rendering.

        Pass `issues` (already gathered by the eval loop) to avoid re-running
        `collect_issues` — it records temperature history as a side effect, so it
        must not be called a second time per pass.
        """
        now_ts = time.time()
        now_lt = time.localtime(now_ts)
        if hp is None:
            hp = self.heatpump_state()
        if mode is None:
            mode = cooling.desired_mode(self.season_cfg, hp)
        if issues is None:
            issues = self.collect_issues(mode, now_ts, now_lt, hp)
        n_alert = sum(1 for i in issues if i.severity == 'alert')
        n_info = sum(1 for i in issues if i.severity == 'info')
        overall = ("OK — nothing to report" if n_alert == 0 and n_info == 0
                   else f"{n_alert} alert(s), {n_info} note(s)")

        hp_line = None
        hp_head = None        # web: always-visible summary line
        hp_rows = []          # web: collapsed detail table [(label, value), ...]
        if hp:
            t = hp['telemetry']
            units = {'vorlauf': '°C', 'ruecklauf': '°C', 'outdoor': '°C',
                     'power': 'W', 'pressure': 'bar'}
            labels = {'vorlauf': 'Flow (Vorlauf)', 'ruecklauf': 'Return (Rücklauf)',
                      'outdoor': 'Outdoor', 'power': 'Power', 'pressure': 'Pressure'}
            parts = [f"{k} {self._fmt(t[k], units.get(k, ''))}"
                     for k in ('vorlauf', 'ruecklauf', 'outdoor', 'power', 'pressure')
                     if k in t]
            hp_rows = [(labels[k], self._fmt(t[k], units.get(k, '')))
                       for k in ('vorlauf', 'ruecklauf', 'outdoor', 'power', 'pressure')
                       if k in t]
            # the pump's own dew-point view (flow floor = dewtemperature +
            # dewoffset) — the binding constraint on how cold it can cool. Show
            # the temp+humidity ems-esp derived it from (the values we feed it as
            # remotetemp/remotehum, echoed back as rftemp/currtemp + airhumidity).
            raw = hp.get('raw') or {}
            dewt = raw.get('dewtemperature')
            if dewt is not None:
                seg = f"dew {self._fmt(dewt, '°C')}"
                dval = self._fmt(dewt, '°C')
                rt = raw.get('rftemp', raw.get('currtemp'))
                ah = raw.get('airhumidity')
                if rt is not None and ah is not None:
                    detail = f" (ems-esp {self._fmt(rt, '°C')} {self._fmt(ah, '%RH')})"
                    seg += detail
                    dval += detail
                parts.append(seg)
                hp_rows.append(('Dew point', dval))
            act = 'running' if hp.get('active') else 'idle'
            # `hpactivity` is what the pump is doing *right now* (e.g. "hot
            # water", "cooling", "heating", "off") — distinct from the season
            # mode. During a domestic-hot-water charge it reverses to heating,
            # so flow/return read hot even though the season is cooling; showing
            # the activity stops that looking like a sensor fault.
            activity = (hp.get('raw') or {}).get('hpactivity')
            head = (f"{hp['mode']} season — {activity} ({act})"
                    if activity else f"{hp['mode']} ({act})")
            hp_head = head
            hp_line = f"{head}: " + ", ".join(parts)

        manual_line = None
        if self.manual_thermostats:
            want = "OPEN fully" if mode == 'cooling' else "normal heating"
            manual_line = f"{want}: " + ", ".join(self.manual_thermostats)

        window_line = None        # text/mail: single line
        window_head = None        # web: summary (which rooms) — always visible
        window_rows = []          # web: collapsed detail [(room, age), ...]
        if self.window_off:
            items = sorted(self.window_off.items())
            window_line = ", ".join(f"{r} ({self._age(ts)})" for r, ts in items)
            window_head = ", ".join(r for r, _ in items)
            window_rows = [(r, self._age(ts)) for r, ts in items]

        fan_line = None
        if self.fan_cfg.get('enabled') and self.fans:
            fan_line = (f"{'ON' if self._fans_on else 'OFF'} ({len(self.fans)} fan(s)) "
                        f"— cooling {'active' if self._cool_active else 'idle'}")

        thermo_bat_limit = self.alerts_cfg.get('battery_limit', 20)
        records, setpoints = [], []
        for name, item in self.thermostats.items():
            st = self.last_state.get(name) or {}
            type_cfg = self.thermostat_types.get(item.get('type'), {})
            sp = current_setpoint(item, now_lt, mode, self.season_cfg)
            setpoints.append(sp)
            temp_state = self._room_temp_state(name)
            temp = temp_state.get('temperature') if isinstance(temp_state, dict) else None
            # Show *our* state vocabulary, not the device's raw mode fields.
            if name in self.window_off:
                state_cell = "off (window)"   # we hold it off (latch wins over classify)
            else:
                state_cell = self._OUR_STATE.get(
                    cooling.classify_state(type_cfg, st), "—")
            style = {}
            tol = item.get('tolerance', self.cfg.get('default_tolerance', 1.5))
            if isinstance(temp, (int, float)) and isinstance(sp, (int, float)):
                # highlight the deviation that matters for the season: too warm
                # while cooling, too cold while heating
                if mode == 'cooling' and temp > sp + tol:
                    style['temp'] = self._CSS_HOT
                elif mode == 'heating' and temp < sp - tol:
                    style['temp'] = self._CSS_COLD
            if self._battery_low(st, thermo_bat_limit):
                style['bat'] = self._CSS_BAD
            records.append((name, state_cell, sp, temp,
                            st.get('battery'), self._age(self.last_seen.get(name)),
                            style))

        # Set points live in the overview, not as a table column: uniform (cooling's
        # single open target) -> one summary line; per-room (heating's day/night
        # schedule points) -> a collapsible fold (range summary + per-room detail).
        thermo_headers = ["room", "state", "temp", "bat", "seen"]
        thermo_rows = [[n, sc, self._fmt(t, "°C"), self._fmt(b, "%"), age]
                       for (n, sc, sp, t, b, age, _) in records]
        thermo_styles = [r[6] for r in records]

        seen_sp = [s for s in setpoints if s is not None]
        uniform_sp = bool(seen_sp) and len(set(seen_sp)) == 1
        set_line = set_head = None
        set_rows = []
        if uniform_sp:
            set_line = self._fmt(seen_sp[0], "°C")
        elif seen_sp:
            set_head = f"{min(seen_sp):g}–{max(seen_sp):g}°C"
            set_rows = [(n, self._fmt(sp, "°C")) for (n, sc, sp, t, b, age, _)
                        in records if sp is not None]

        sensor_rows = sensor_styles = None
        if self.sensor_kind:
            sensor_bat_limit = self.sensors_cfg.get('battery_limit', 20)
            sensor_rows, sensor_styles = [], []
            for name, kind in self.sensor_kind.items():
                st = self.sensor_state.get(name) or {}
                style = {}
                if not st:
                    val = "—"
                elif kind == 'window':
                    if sensors_mod.window_open(st):
                        val = "OPEN"
                        style['value'] = self._CSS_WARN
                    else:
                        val = "closed"
                elif kind == 'leak':
                    if st.get('water_leak'):
                        val = "LEAK"
                        style['value'] = self._CSS_BAD
                    else:
                        val = "dry"
                else:
                    val = self._fmt(st.get('temperature'), "°C")
                    hum = st.get('humidity')
                    if hum is not None:
                        dp = dew_point(st.get('temperature'), hum)
                        val = f"{val} {self._fmt(hum, '%RH')}"
                        if dp is not None:
                            val = f"{val} dp {self._fmt(round(dp, 1), '°C')}"
                if self._battery_low(st, sensor_bat_limit):
                    style['bat'] = self._CSS_BAD
                sensor_rows.append([name, val, kind,
                                    self._fmt(st.get('battery'), "%"),
                                    self._age(self.sensor_seen.get(name))])
                sensor_styles.append(style)

        order = {'alert': 0, 'info': 1}
        issue_list = [(i.severity, i.subject, i.detail)
                      for i in sorted(issues, key=lambda x: order.get(x.severity, 2))]

        return {
            'when': time.strftime('%a %Y-%m-%d %H:%M', now_lt),
            'overall': overall, 'n_alert': n_alert, 'n_info': n_info,
            'mode': mode,
            'hp_line': hp_line, 'hp_head': hp_head, 'hp_rows': hp_rows,
            'manual_line': manual_line,
            'window_line': window_line, 'window_head': window_head,
            'window_rows': window_rows, 'fan_line': fan_line,
            'set_line': set_line, 'set_head': set_head, 'set_rows': set_rows,
            'thermo': {'headers': thermo_headers,
                       'rows': thermo_rows, 'styles': thermo_styles},
            'sensors': ({'headers': ["sensor", "value", "kind", "bat", "seen"],
                         'rows': sensor_rows, 'styles': sensor_styles}
                        if sensor_rows is not None else None),
            'issues': issue_list,
        }

    # our state vocabulary (not the device's raw mode fields) for the report
    _OUR_STATE = {'open': 'open', 'off': 'off', 'schedule': 'schedule',
                  'manual': 'MANUAL', 'unknown': '—'}

    # cell highlight styles for the HTML report
    _CSS_BAD = 'color:#b00020;font-weight:bold'      # low battery / leak
    _CSS_HOT = 'color:#c0392b;font-weight:bold'      # too warm vs target
    _CSS_COLD = 'color:#1f6feb;font-weight:bold'     # too cold vs target
    _CSS_WARN = 'color:#c05600;font-weight:bold'     # window open

    @staticmethod
    def _battery_low(state, limit):
        if not isinstance(state, dict):
            return False
        if state.get('battery_low') is True:
            return True
        lvl = state.get('battery')
        return isinstance(lvl, (int, float)) and lvl < limit

    @staticmethod
    def _render_text(d):
        bar = "=" * 56
        lines = [bar, "  Thermostat status report", f"  {d['when']}", bar,
                 f"  Overall : {d['overall']}", f"  Mode    : {d['mode']}"]
        if d['hp_line']:
            lines.append(f"  Heatpump: {d['hp_line']}")
        if d['manual_line']:
            lines.append(f"  Manual valves -> {d['manual_line']}")
        if d.get('window_line'):
            lines.append(f"  Off (window open) -> {d['window_line']}")
        if d.get('fan_line'):
            lines.append(f"  Fans: {d['fan_line']}")
        if d.get('set_line'):
            lines.append(f"  Set point: {d['set_line']} (all rooms)")
        elif d.get('set_head'):
            lines.append(f"  Set points ({d['set_head']}): "
                         + ", ".join(f"{r} {v}" for r, v in d['set_rows']))
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
    def _html_table(headers, rows, styles=None):
        """One scrollable HTML table (horizontal scroll on narrow screens).

        `styles` is an optional per-row list of {header_name: extra_css} maps
        used to colour individual cells (low battery, open window, off-target).
        """
        th = "".join(
            '<th style="text-align:left;padding:4px 12px 4px 0;'
            'border-bottom:2px solid #999;white-space:nowrap">'
            f'{html.escape(str(h))}</th>' for h in headers)
        trs = []
        for ri, row in enumerate(rows):
            cs = styles[ri] if styles else {}
            tds = "".join(
                f'<td style="padding:3px 12px 3px 0;border-bottom:1px solid #e2e2e2;'
                f'white-space:nowrap;{cs.get(h, "")}">{html.escape(str(c))}</td>'
                for h, c in zip(headers, row))
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
        if d.get('window_line'):
            p.append('<tr><td style="padding-right:12px;color:#777;'
                     f'vertical-align:top">Off (window open)</td><td>{esc(d["window_line"])}'
                     '</td></tr>')
        if d.get('set_line'):
            p.append('<tr><td style="padding-right:12px;color:#777">Set point</td>'
                     f'<td>{esc(d["set_line"])} <span style="color:#777">'
                     '(all rooms)</span></td></tr>')
        elif d.get('set_head'):
            detail = ", ".join(f"{r} {v}" for r, v in d['set_rows'])
            p.append('<tr><td style="padding-right:12px;color:#777;vertical-align:top">'
                     f'Set points</td><td>{esc(d["set_head"])} '
                     f'<span style="color:#777">({esc(detail)})</span></td></tr>')
        p.append('</tbody></table>')
        p.append('<h3 style="font-size:15px;margin:14px 0 2px">Thermostats</h3>')
        p.append(Manager._html_table(d['thermo']['headers'], d['thermo']['rows'],
                                     d['thermo'].get('styles')))
        if d['sensors']:
            p.append('<h3 style="font-size:15px;margin:14px 0 2px">Sensors</h3>')
            p.append(Manager._html_table(d['sensors']['headers'], d['sensors']['rows'],
                                         d['sensors'].get('styles')))
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

    def web_page(self):
        """Full standalone HTML page for the status web server, from the cached
        snapshot. Returns a 'warming up' placeholder until the first eval pass."""
        return self._render_web(self._last_report,
                                refresh=self.web_cfg.get('refresh', 60),
                                link_rooms=True)

    # Project logo (also attached to the ThermostatScheduler Trac page). Served at
    # /logo.svg and shown in the web UI header + as favicon (coding standard §5).
    # The PNG is the iOS "Add to Home Screen" app icon (apple-touch-icon, 180x180).
    _DOCS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'docs')
    _LOGO_PATH = os.path.join(_DOCS_DIR, 'thermostat.svg')
    _APPLE_ICON_PATH = os.path.join(_DOCS_DIR, 'apple-touch-icon.png')
    _logo_cache = None
    _apple_icon_cache = None
    # favicon + iOS home-screen icon + web-app title; shared by every page <head>
    _ICON_LINKS = ('<link rel="icon" type="image/svg+xml" href="/logo.svg">'
                   '<link rel="apple-touch-icon" href="/apple-touch-icon.png">'
                   '<meta name="apple-mobile-web-app-title" content="Klima Status">'
                   '<meta name="apple-mobile-web-app-capable" content="yes">')

    @classmethod
    def logo_svg(cls):
        if cls._logo_cache is None:
            try:
                with open(cls._LOGO_PATH, encoding='utf-8') as f:
                    cls._logo_cache = f.read()
            except OSError:
                cls._logo_cache = ''      # missing logo must never break the page
        return cls._logo_cache

    @classmethod
    def apple_icon_png(cls):
        if cls._apple_icon_cache is None:
            try:
                with open(cls._APPLE_ICON_PATH, 'rb') as f:
                    cls._apple_icon_cache = f.read()
            except OSError:
                cls._apple_icon_cache = b''
        return cls._apple_icon_cache

    @staticmethod
    def room_url(room, hours=history.DEFAULT_HOURS):
        return '/room?' + urllib.parse.urlencode({'name': room, 'hours': hours})

    def _trv_friendly(self, room):
        """The room TRV's current friendly name (topic minus base, registry-resolved)."""
        topic = self.thermo_sub_topic.get(room, '')
        prefix = f"{self.base}/"
        return topic[len(prefix):] if topic.startswith(prefix) else None

    def _room_chart_spec(self, room):
        """Build the history-chart entity-candidate spec for a room from the
        ieee-anchored device maps. Each device contributes HA entity candidates
        (named slug first, then its 0x<ieee> form) so a sensor HA logged under its
        raw ieee still resolves — no per-room override needed."""
        temp_label = self.room_temp_sensor.get(room)
        if temp_label:
            temp_cands = devices.ha_entity_candidates(
                self.sensor_ieee.get(temp_label),
                self.sensor_friendly.get(temp_label), 'temperature')
        else:
            # no room sensor -> the TRV's own local_temperature (comfort fallback)
            temp_cands = devices.ha_entity_candidates(
                self.thermo_ieee.get(room), self._trv_friendly(room),
                'local_temperature')
        windows = [devices.ha_entity_candidates(self.sensor_ieee.get(w),
                                                self.sensor_friendly.get(w), 'contact')
                   for w in self.room_windows.get(room, [])]
        return {'temp': temp_cands, 'outdoor': history.HP_OUTDOOR_TEMP,
                'activity': history.HP_ACTIVITY, 'windows': windows}

    def room_page(self, room, hours=history.DEFAULT_HOURS):
        """Standalone HTML page with the last `hours` of history for one room.

        Queries HA's InfluxDB on demand (the only place the web server touches
        external state). `room` must be a configured thermostat; `hours` must be one
        of history.HOURS_CHOICES — both are validated by the caller (the HTTP route).
        """
        esc = html.escape
        item = self.thermostats.get(room)
        if item is None:
            return None
        if self._influx is None:
            self._influx = history.InfluxClient(
                url=self._history_cfg.get('influx_url', 'http://job4:8086'),
                database=self._history_cfg.get('database', 'homeassistant'))
        data = history.collect_room_history(self._influx, self._room_chart_spec(room),
                                            hours)
        svg = history.render_room_svg(room, data)

        toggles = ' '.join(
            (f'<strong>{history.hours_label(h)}</strong>' if h == hours
             else f'<a href="{self.room_url(room, h)}">{history.hours_label(h)}</a>')
            for h in history.HOURS_CHOICES)
        p = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             f'<title>{esc(room)} history</title>',
             self._ICON_LINKS,
             f'<style>{self._WEB_CSS}.toggles{{font-size:13px;margin:0 0 14px}}'
             '.toggles a{margin-right:10px}.toggles strong{margin-right:10px}'
             '.back{font-size:13px}</style></head><body><div class="wrap">',
             f'<header><img class="logo" src="/logo.svg" alt="">'
             f'<h1>{esc(room)}</h1>'
             f'<span class="when">last {history.hours_label(hours)}</span></header>',
             f'<div class="toggles">range: {toggles} '
             f'&nbsp;·&nbsp; <a class="back" href="/">← all rooms</a></div>',
             f'<div class="card">{svg}</div>',
             '</div></body></html>']
        return ''.join(p)

    # --- status web page (standalone, class-based layout) -------------------

    _WEB_CSS = """
:root{color-scheme:light dark}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
 background:#f4f5f7;color:#1f2329;line-height:1.45}
.wrap{max-width:920px;margin:0 auto;padding:20px 16px 48px}
header{display:flex;align-items:center;gap:8px 10px;flex-wrap:wrap;margin-bottom:16px}
header .logo{height:30px;width:30px;flex:0 0 auto}
h1{font-size:22px;margin:0;font-weight:650}
.when{color:#6b7280;font-size:13px}
.pill{display:inline-block;padding:3px 12px;border-radius:999px;font-size:13px;
 font-weight:650;color:#fff;margin-left:auto}
.pill.ok{background:#1a7f37}.pill.note{background:#bf8700}.pill.alert{background:#b42318}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:16px 18px;
 margin:0 0 16px;box-shadow:0 1px 2px rgba(0,0,0,.04)}
.card h2{font-size:14px;text-transform:uppercase;letter-spacing:.04em;color:#6b7280;
 margin:0 0 12px;font-weight:650}
.kv{display:grid;grid-template-columns:max-content 1fr;gap:6px 18px;font-size:14px}
.kv .k{color:#6b7280}
details.fold{font-size:14px;margin-top:8px;border-top:1px solid #f0f1f3;padding-top:8px}
details.fold summary{cursor:pointer;list-style:none}
details.fold summary::-webkit-details-marker{display:none}
details.fold summary::before{content:"\\25B8";color:#9ca3af;margin-right:7px;
 display:inline-block;transition:transform .15s}
details.fold[open] summary::before{transform:rotate(90deg)}
details.fold .k{color:#6b7280;margin-right:6px}
table.fold-tbl{width:auto;margin:8px 0 2px 20px;font-size:13.5px}
table.fold-tbl td{padding:3px 16px 3px 0;border:none;white-space:nowrap}
table.fold-tbl td.k{color:#6b7280}
.tbl-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;font-size:14px}
th{text-align:left;font-weight:650;color:#6b7280;border-bottom:2px solid #e5e7eb;
 padding:6px 14px 6px 0;white-space:nowrap}
td{padding:7px 14px 7px 0;border-bottom:1px solid #f0f1f3;white-space:nowrap}
tbody tr:last-child td{border-bottom:none}
ul.issues{list-style:none;margin:0;padding:0;font-size:14px}
ul.issues li{padding:7px 0;border-bottom:1px solid #f0f1f3}
ul.issues li:last-child{border-bottom:none}
.tag{display:inline-block;min-width:46px;text-align:center;padding:1px 7px;border-radius:6px;
 font-size:12px;font-weight:700;margin-right:8px}
.tag.alert{background:#fde7e4;color:#b42318}.tag.note{background:#eef1f4;color:#57606a}
footer{color:#9ca3af;font-size:12px;text-align:center;margin-top:8px}
@media(prefers-color-scheme:dark){
 body{background:#0f1115;color:#e6e8eb}
 .card{background:#181b21;border-color:#2a2f37;box-shadow:none}
 th{border-color:#2a2f37}td{border-color:#23272e}ul.issues li{border-color:#23272e}
 .kv .k,.when,.card h2,footer{color:#9aa3ad}
 .tag.note{background:#23272e;color:#aeb6bf}.tag.alert{background:#3a1d1a;color:#ff8a7a}}
"""

    @staticmethod
    def _web_table(headers, rows, styles=None, links=None):
        """Render an HTML table. `links` is an optional per-row {header: url} map;
        a matching cell is wrapped in an <a> (used to link a room's temp to its
        history chart). Cell text is always escaped."""
        esc = html.escape
        th = "".join(f'<th>{esc(str(h))}</th>' for h in headers)
        trs = []
        for ri, row in enumerate(rows):
            cs = styles[ri] if styles else {}
            ls = links[ri] if links else {}
            cells = []
            for h, c in zip(headers, row):
                inner = esc(str(c))
                if ls.get(h):
                    inner = f'<a href="{esc(ls[h])}">{inner}</a>'
                style = f' style="{cs[h]}"' if cs.get(h) else ''
                cells.append(f'<td{style}>{inner}</td>')
            trs.append(f"<tr>{''.join(cells)}</tr>")
        return (f'<div class="tbl-wrap"><table><thead><tr>{th}</tr></thead>'
                f'<tbody>{"".join(trs)}</tbody></table></div>')

    @classmethod
    def _render_web(cls, d, refresh=60, link_rooms=False):
        esc = html.escape
        head = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
                '<meta name="viewport" content="width=device-width,initial-scale=1">']
        if refresh and refresh > 0:
            head.append(f'<meta http-equiv="refresh" content="{int(refresh)}">')
        head.append('<title>Klima Status</title>')
        head.append(cls._ICON_LINKS)
        head.append(f'<style>{cls._WEB_CSS}</style></head><body><div class="wrap">')
        logo = '<img class="logo" src="/logo.svg" alt="">'

        if not d:
            head.append(f'<header>{logo}<h1>Klima Status</h1></header>'
                        '<div class="card">Starting up — no data yet. '
                        'This page refreshes automatically.</div>'
                        '</div></body></html>')
            return "".join(head)

        cls_pill = ('alert' if d['n_alert'] else 'note' if d['n_info'] else 'ok')
        p = head
        # Status pill rides on the right of the title bar when there's room (desktop)
        # and wraps below the title on narrow screens.
        p.append(f'<header>{logo}<h1>Klima Status</h1>'
                 f'<span class="when">{esc(d["when"])}</span>'
                 f'<span class="pill {cls_pill}">{esc(d["overall"])}</span></header>')

        # summary card
        p.append('<div class="card"><h2>Overview</h2><div class="kv">')
        p.append(f'<div class="k">Mode</div><div>{esc(d["mode"])}</div>')
        if d.get('set_line'):
            p.append(f'<div class="k">Set point</div>'
                     f'<div>{esc(d["set_line"])} (all rooms)</div>')
        if d['manual_line']:
            p.append(f'<div class="k">Manual valves</div>'
                     f'<div>{esc(d["manual_line"])}</div>')
        if d.get('fan_line'):
            p.append(f'<div class="k">Fans</div><div>{esc(d["fan_line"])}</div>')
        p.append('</div>')

        # Compact, collapsible sub-sections: a summary line is always visible, the
        # detail table expands on tap (same idiom for heat pump + open windows).
        def _fold(label, head, rows):
            body = ''.join(f'<tr><td class="k">{esc(a)}</td><td>{esc(b)}</td></tr>'
                           for a, b in rows)
            return (f'<details class="fold"><summary><span class="k">{esc(label)}'
                    f'</span> {esc(head)}</summary>'
                    f'<table class="fold-tbl">{body}</table></details>')

        if d.get('set_head'):       # per-room set points (heating schedule)
            p.append(_fold('Set point', d['set_head'], d.get('set_rows') or []))
        if d.get('hp_head'):
            p.append(_fold('Heat pump', d['hp_head'], d.get('hp_rows') or []))
        if d.get('window_head'):
            p.append(_fold('Off (window open)', d['window_head'],
                           d.get('window_rows') or []))
        p.append('</div>')

        p.append('<div class="card"><h2>Thermostats</h2>')
        thermo_links = None
        if link_rooms:
            # link each room's name + temperature cell to its history chart
            thermo_links = [{'room': cls.room_url(row[0]), 'temp': cls.room_url(row[0])}
                            for row in d['thermo']['rows']]
        p.append(cls._web_table(d['thermo']['headers'], d['thermo']['rows'],
                                d['thermo'].get('styles'), thermo_links))
        p.append('</div>')

        if d['sensors']:
            p.append('<div class="card"><h2>Sensors</h2>')
            p.append(cls._web_table(d['sensors']['headers'], d['sensors']['rows'],
                                    d['sensors'].get('styles')))
            p.append('</div>')

        p.append('<div class="card"><h2>Open items</h2>')
        if d['issues']:
            p.append('<ul class="issues">')
            for sev, subject, detail in d['issues']:
                tag = 'alert' if sev == 'alert' else 'note'
                label = 'ALERT' if sev == 'alert' else 'note'
                p.append(f'<li><span class="tag {tag}">{label}</span>'
                         f'<strong>{esc(subject)}</strong>: {esc(detail)}</li>')
            p.append('</ul>')
        else:
            p.append('<div style="color:#6b7280;font-size:14px">'
                     'Nothing to report.</div>')
        p.append('</div>')

        p.append(f'<footer>auto-refreshes every {int(refresh)}s</footer>')
        p.append('</div></body></html>')
        return "".join(p)

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
            topic = self._trv_set_topic(name)
            try:
                client.publish(topic, json.dumps(payload), qos=1)
                self.applied_mode[name] = want
                log.info("cooling: set %s -> %s (%s)", name, want, payload)
            except Exception as e:
                log.error("cooling publish failed for %s: %s", name, e)


def start_web_server(mgr):
    """Start a background HTTP server that serves the status page (read-only).

    Serves the manager's cached snapshot, so requests never touch MQTT state or
    record history. Returns the server (or None if disabled / failed to bind)."""
    import http.server

    host = mgr.web_cfg.get('host', '0.0.0.0')
    port = int(mgr.web_cfg.get('port', 8099))

    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, body):
            body = body.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def _send_asset(self, body, ctype):
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'max-age=86400')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == '/logo.svg':
                self._send_asset(mgr.logo_svg().encode('utf-8'),
                                 'image/svg+xml; charset=utf-8')
                return
            if parsed.path == '/apple-touch-icon.png':
                self._send_asset(mgr.apple_icon_png(), 'image/png')
                return
            if parsed.path == '/room':
                qs = urllib.parse.parse_qs(parsed.query)
                room = (qs.get('name') or [''])[0]
                # whitelist room + hours so no caller string reaches a query
                if room not in mgr.thermostats:
                    self.send_error(404)
                    return
                try:
                    hours = int((qs.get('hours') or [history.DEFAULT_HOURS])[0])
                except ValueError:
                    hours = history.DEFAULT_HOURS
                if hours not in history.HOURS_CHOICES:
                    hours = history.DEFAULT_HOURS
                self._send(mgr.room_page(room, hours))
                return
            if parsed.path not in ('/', '/index.html', '/status'):
                self.send_error(404)
                return
            self._send(mgr.web_page())

        def log_message(self, *a):
            pass   # don't spam the journal with one line per request

    try:
        httpd = http.server.ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        log.error("status web server: cannot bind %s:%s (%s)", host, port, e)
        return None
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log.info("status web page at http://%s:%s/", host, port)
    return httpd


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
        mgr.publish_remote_feed(client)
        client.loop_stop()
        client.disconnect()
        return

    # Flush device state on a systemd stop/restart (SIGTERM) too, not just Ctrl-C.
    import signal

    def _graceful(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _graceful)

    if mgr.web_cfg.get('enabled'):
        start_web_server(mgr)

    # Heat-pump remote feed runs on its own (faster) cadence than the eval loop.
    rf = mgr.remote_feed_cfg
    if rf.get('enabled'):
        interval = rf.get('interval', 60)

        def _feed_loop():
            while True:
                try:
                    mgr.publish_remote_feed(client)
                except Exception as e:
                    log.exception("remote feed error: %s", e)
                time.sleep(interval)
        threading.Thread(target=_feed_loop, daemon=True).start()
        cands = ", ".join(c['sensor'] for c in mgr._remote_feed_candidates())
        offset = mgr.remote_feed_cfg.get('temp_offset', 0) or 0
        off_note = f", temp_offset {offset:+g}K" if offset else ""
        log.info("remote feed: %d candidate(s) [%s], republishing warmest every %ds%s",
                 len(mgr._remote_feed_candidates()), cands, interval, off_note)

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
