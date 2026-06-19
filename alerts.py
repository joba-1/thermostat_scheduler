#!/usr/bin/env python3
"""
Low-noise operator alerting.

An `Issue` is anything worth telling the operator about. The `Alerter`
deduplicates issues by a stable `key`, mails each new `alert`-severity issue
once, re-mails an ongoing one only after a cooldown, notes cleared issues, and
sends a daily digest of everything still open. State persists to a JSON file so
a daemon restart does not re-spam. `info`-severity issues never trigger an
immediate mail; they only appear in the digest.

Delivery uses the `send-mail` helper (`~/bin/send-mail.py`). A custom sender
callable can be injected for testing.
"""

import json
import os
import sys
import time
import subprocess
from collections import namedtuple

from common import log

# severity: 'alert' (mail immediately) or 'info' (digest only)
Issue = namedtuple('Issue', ['key', 'kind', 'severity', 'subject', 'detail'])


def make_issue(key, kind, subject, detail, severity='alert'):
    return Issue(key=key, kind=kind, severity=severity, subject=subject, detail=detail)


def _default_sender(subject, body, mail_to, send_mail_cmd, from_name=None):
    """Send a mail via the send-mail helper. Returns True on success."""
    try:
        cmd = [send_mail_cmd, subject]
        if mail_to:
            cmd += ['--to', mail_to]
        if from_name:
            cmd += ['--from-name', from_name]
        proc = subprocess.run(cmd, input=body.encode('utf-8'),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            log.error("send-mail failed (%s): %s", proc.returncode,
                      proc.stderr.decode('utf-8', 'ignore').strip())
            return False
        return True
    except Exception as e:
        log.error("send-mail error: %s", e)
        return False


class Alerter:
    def __init__(self, alerts_cfg, sender=None, now_fn=time.time):
        cfg = alerts_cfg or {}
        self.enabled = cfg.get('enabled', True)
        self.mail_to = cfg.get('mail_to')
        self.send_mail_cmd = os.path.expanduser(
            cfg.get('send_mail_cmd', '~/bin/send-mail.py'))
        self.cooldown = cfg.get('cooldown_hours', 24) * 3600
        self.digest_hour = cfg.get('digest_hour', 7)
        self.state_file = os.path.expanduser(
            cfg.get('state_file', '~/.local/state/thermostat_manager/alerts.json'))
        # Sender display name: configured value, else the running script/service
        # name without extension (e.g. "thermostat_monitor") so mail isn't "me".
        self.from_name = cfg.get('from_name') or (
            os.path.splitext(os.path.basename(sys.argv[0]))[0] or 'thermostat')
        self._sender = sender or (lambda subj, body: _default_sender(
            subj, body, self.mail_to, self.send_mail_cmd, self.from_name))
        self._now = now_fn
        self.state = self._load()

    # ---- persistence -------------------------------------------------
    def _load(self):
        try:
            with open(self.state_file) as f:
                return json.load(f)
        except Exception:
            return {'issues': {}, 'last_digest_day': None}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            tmp = self.state_file + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(self.state, f, indent=2)
            os.replace(tmp, self.state_file)
        except Exception as e:
            log.warning("could not persist alert state: %s", e)

    # ---- core --------------------------------------------------------
    def process(self, issues):
        """Reconcile the currently observed issues with stored state.

        Mails new/cooled-down alert-severity issues, records cleared ones, and
        updates the open-issue table. Returns (mailed, cleared) for logging.
        """
        now = self._now()
        open_state = self.state.setdefault('issues', {})
        seen = {iss.key: iss for iss in issues}

        to_mail = []
        for key, iss in seen.items():
            st = open_state.get(key)
            if st is None:
                open_state[key] = {
                    'kind': iss.kind, 'severity': iss.severity,
                    'subject': iss.subject, 'detail': iss.detail,
                    'first': now, 'last_alert': 0,
                }
                if iss.severity == 'alert':
                    open_state[key]['last_alert'] = now
                    to_mail.append(iss)
            else:
                st.update(kind=iss.kind, severity=iss.severity,
                          subject=iss.subject, detail=iss.detail)
                if iss.severity == 'alert' and (now - st.get('last_alert', 0)) >= self.cooldown:
                    st['last_alert'] = now
                    to_mail.append(iss)

        cleared = [k for k in list(open_state.keys()) if k not in seen]
        resolved = []
        for k in cleared:
            st = open_state.pop(k, None)
            # Only announce recovery for issues that actually alerted (loud
            # enough to warn about -> loud enough to confirm resolved). Cleared
            # info-only items stay quiet.
            if st and st.get('severity') == 'alert' and st.get('last_alert'):
                resolved.append(st)

        if to_mail and self.enabled:
            self._mail_issues(to_mail)
        if resolved and self.enabled:
            self._mail_resolved(resolved)
        self._save()
        return to_mail, cleared

    def _mail_issues(self, issues):
        n = len(issues)
        subject = (f"[thermostat] {issues[0].subject}: {issues[0].detail}"
                   if n == 1 else f"[thermostat] {n} new alerts")
        lines = ["The thermostat manager detected the following new issue(s):", ""]
        for iss in issues:
            lines.append(f"  - {iss.subject}: {iss.detail}")
        lines += ["", "You will get a daily digest while these remain open,",
                  "and another mail if an issue persists past the cooldown."]
        self._sender(subject, "\n".join(lines))

    def _mail_resolved(self, resolved):
        n = len(resolved)
        subject = (f"[thermostat] resolved: {resolved[0]['subject']}"
                   if n == 1 else f"[thermostat] {n} issues resolved")
        lines = ["The following thermostat issue(s) have cleared:", ""]
        for st in resolved:
            lines.append(f"  - {st.get('subject')}: {st.get('detail')} — now OK")
        self._sender(subject, "\n".join(lines))

    def maybe_send_digest(self, localtime_fn=time.localtime):
        """Send the daily digest once, when the configured hour is reached."""
        if not self.enabled:
            return False
        lt = localtime_fn(self._now())
        day = time.strftime('%Y-%m-%d', lt)
        if lt.tm_hour < self.digest_hour:
            return False
        if self.state.get('last_digest_day') == day:
            return False
        self.state['last_digest_day'] = day
        open_issues = self.state.get('issues', {})
        self._send_digest(open_issues, day)
        self._save()
        return True

    def notify(self, subject, body):
        """Send an arbitrary mail through the configured sender (if enabled)."""
        if not self.enabled:
            return False
        return self._sender(subject, body)

    def due(self, key, interval_seconds):
        """Return True at most once per `interval_seconds` for `key`.

        Used for the periodic status report. The last-fire time is persisted in
        the alert state file so it survives daemon restarts.
        """
        now = self._now()
        marks = self.state.setdefault('periodic', {})
        last = marks.get(key, 0)
        if now - last < interval_seconds:
            return False
        marks[key] = now
        self._save()
        return True

    def _send_digest(self, open_issues, day):
        if not open_issues:
            # Nothing open: a quiet "all clear" once a day is reassuring but
            # easy to filter; keep it short.
            self._sender(f"[thermostat] daily digest {day}: all clear",
                         "No open thermostat/sensor/heat-pump issues. All good.")
            return
        lines = [f"Open thermostat issues as of {day}:", ""]
        for key, st in sorted(open_issues.items()):
            sev = st.get('severity', 'info').upper()
            lines.append(f"  [{sev}] {st.get('subject')}: {st.get('detail')}")
        self._sender(f"[thermostat] daily digest {day}: {len(open_issues)} open",
                     "\n".join(lines))
