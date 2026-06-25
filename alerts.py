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

import html
import json
import os
import sys
import time
import tempfile
import subprocess
from collections import namedtuple

from common import log

# severity: 'alert' (mail immediately) or 'info' (digest only)
Issue = namedtuple('Issue', ['key', 'kind', 'severity', 'subject', 'detail'])


def make_issue(key, kind, subject, detail, severity='alert'):
    return Issue(key=key, kind=kind, severity=severity, subject=subject, detail=detail)


def _default_sender(subject, body, mail_to, send_mail_cmd, from_name=None,
                    html_body=None):
    """Send a mail via the send-mail helper. Returns True on success.

    With `html_body`, a rich HTML alternative is sent (used for the status
    report's scrollable tables); otherwise a monospace `--pre` part keeps simple
    preformatted lists aligned.
    """
    html_path = None
    try:
        cmd = [send_mail_cmd, subject]
        if mail_to:
            cmd += ['--to', mail_to]
        if from_name:
            cmd += ['--from-name', from_name]
        if html_body:
            fd, html_path = tempfile.mkstemp(suffix='.html')
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(html_body)
            cmd += ['--html-file', html_path]
        else:
            cmd += ['--pre']
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
    finally:
        if html_path:
            try:
                os.unlink(html_path)
            except OSError:
                pass


class Alerter:
    def __init__(self, alerts_cfg, sender=None, now_fn=time.time):
        cfg = alerts_cfg or {}
        self.enabled = cfg.get('enabled', True)
        self.mail_to = cfg.get('mail_to')
        self.send_mail_cmd = os.path.expanduser(
            cfg.get('send_mail_cmd', '~/bin/send-mail.py'))
        self.cooldown = cfg.get('cooldown_hours', 24) * 3600
        self.digest_hour = cfg.get('digest_hour', 7)
        # Coalescing window: issues are collected across eval passes and mailed in
        # one combined attempt at most once per window. A burst of new issues, and
        # any issue that opens then clears again within the window, collapse into a
        # single mail (or none) instead of one attempt per pass. The first issue
        # after a quiet spell still goes out promptly (the window starts empty).
        self.batch_window = cfg.get('batch_window_minutes', 10) * 60
        # Hard ceiling on *issue* mails per local day. The daily digest is exempt
        # (it always gets through), so total daily volume is digest + this cap.
        # Anything suppressed by the cap still surfaces in the next digest.
        self.max_mails_per_day = cfg.get('max_mails_per_day', 6)
        # Optional callable -> (text, html) full status report, injected by the
        # daemon. When set, every mail carries the current status report so each
        # notification is self-contained ("important issue + full status").
        self.report_provider = None
        self.state_file = os.path.expanduser(
            cfg.get('state_file', '~/.local/state/thermostat_manager/alerts.json'))
        # Sender display name: configured value, else the running script/service
        # name without extension (e.g. "thermostat_monitor") so mail isn't "me".
        self.from_name = cfg.get('from_name') or (
            os.path.splitext(os.path.basename(sys.argv[0]))[0] or 'thermostat')
        self._sender = sender or (lambda subj, body, html_body=None: _default_sender(
            subj, body, self.mail_to, self.send_mail_cmd, self.from_name, html_body))
        self._now = now_fn
        self.state = self._load()

    # ---- persistence -------------------------------------------------
    def _load(self):
        try:
            with open(self.state_file) as f:
                return json.load(f)
        except Exception:
            return {'issues': {}, 'last_digest_day': None}

    # ---- mail budget + unified send ----------------------------------
    def _budget(self):
        """The {day, count} mail-budget record, reset at each local-day rollover."""
        day = time.strftime('%Y-%m-%d', time.localtime(self._now()))
        b = self.state.setdefault('mailbudget', {'day': day, 'count': 0})
        if b.get('day') != day:
            b['day'], b['count'] = day, 0
        return b

    def _emit(self, subject, text, html_body=None, *, essential=False,
              attach_report=True):
        """Single choke point for all outgoing mail.

        Enforces the daily issue-mail cap (non-essential mail beyond the cap is
        dropped and left to the next digest), and appends the full status report
        so every notification stands on its own. `essential` mail (the digest, an
        explicitly requested report) always sends and ignores the cap.
        """
        if not self.enabled:
            return False
        b = self._budget()
        if not essential and b['count'] >= self.max_mails_per_day:
            log.info("mail budget reached (%d/day); suppressing %r (will appear "
                     "in the daily digest)", self.max_mails_per_day, subject)
            return False
        if attach_report and self.report_provider:
            try:
                rep = self.report_provider()
            except Exception as e:
                log.warning("report_provider failed: %s", e)
                rep = None
            if rep:
                rep_text, rep_html = rep
                if rep_text:
                    text = f"{text}\n\n{'=' * 60}\n{rep_text}"
                if html_body and rep_html:
                    html_body = f"{html_body}<hr style='margin:18px 0'>{rep_html}"
        ok = self._sender(subject, text, html_body)
        if ok:
            b['count'] += 1
            self._save()
        return ok

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

        Records new/cleared issues every pass, but only *mails* on the batch
        boundary (`batch_window`): one combined mail for all alert-severity
        issues still open and due, plus one for recoveries queued since the last
        flush. Returns (mailed, cleared) for logging — `mailed` is empty on passes
        that only collect.
        """
        now = self._now()
        open_state = self.state.setdefault('issues', {})
        seen = {iss.key: iss for iss in issues}

        for key, iss in seen.items():
            st = open_state.get(key)
            if st is None:
                open_state[key] = {
                    'kind': iss.kind, 'severity': iss.severity,
                    'subject': iss.subject, 'detail': iss.detail,
                    'first': now, 'last_alert': 0,
                }
            else:
                st.update(kind=iss.kind, severity=iss.severity,
                          subject=iss.subject, detail=iss.detail)

        cleared = [k for k in list(open_state.keys()) if k not in seen]
        # Recoveries accumulate across passes and flush with the next batch. Only
        # queue issues that actually alerted; an issue that opened and cleared
        # within a window (never mailed) leaves no trace -> no flap mail.
        pending_resolved = self.state.setdefault('pending_resolved', [])
        for k in cleared:
            st = open_state.pop(k, None)
            if st and st.get('severity') == 'alert' and st.get('last_alert'):
                pending_resolved.append(st)

        mailed = []
        if self.enabled and (now - self.state.get('last_batch', 0)) >= self.batch_window:
            to_mail = [iss for key, iss in seen.items()
                       if iss.severity == 'alert' and (
                           open_state[key].get('last_alert', 0) == 0
                           or (now - open_state[key]['last_alert']) >= self.cooldown)]
            if to_mail or pending_resolved:
                if to_mail and self._mail_issues(to_mail):
                    for iss in to_mail:
                        open_state[iss.key]['last_alert'] = now
                    mailed = to_mail
                if pending_resolved and self._mail_resolved(pending_resolved):
                    self.state['pending_resolved'] = []
                self.state['last_batch'] = now
        self._save()
        return mailed, cleared

    @staticmethod
    def html_message(intro, items, outro=None):
        """A clean proportional-font HTML body: intro paragraph + bulleted list.

        Used so alert/digest/recovery mails read like normal email instead of
        the monospace `<pre>` block (which renders as a 'strange font' with odd
        line breaks in many clients). `items` may be plain strings or
        (prefix, text) tuples where the prefix is shown bold/coloured.
        """
        esc = html.escape
        p = ['<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;'
             'color:#222;line-height:1.4">',
             f'<p style="margin:0 0 10px">{esc(intro)}</p>',
             '<ul style="margin:0 0 10px;padding-left:20px">']
        for it in items:
            if isinstance(it, tuple):
                prefix, text = it
                color = '#b00020' if str(prefix).upper() == 'ALERT' else '#777'
                p.append(f'<li style="margin:3px 0"><span style="color:{color};'
                         f'font-weight:bold">[{esc(str(prefix))}]</span> {esc(text)}</li>')
            else:
                p.append(f'<li style="margin:3px 0">{esc(it)}</li>')
        p.append('</ul>')
        if outro:
            p.append(f'<p style="margin:0;color:#555">{esc(outro)}</p>')
        p.append('</div>')
        return "".join(p)

    def _mail_issues(self, issues):
        n = len(issues)
        subject = (f"[thermostat] {issues[0].subject}: {issues[0].detail}"
                   if n == 1 else f"[thermostat] {n} new alerts")
        intro = "The thermostat manager detected the following new issue(s):"
        items = [f"{iss.subject}: {iss.detail}" for iss in issues]
        outro = ("You will get a daily digest while these remain open, "
                 "and another mail if an issue persists past the cooldown.")
        text = "\n".join([intro, ""] + [f"  - {it}" for it in items] + ["", outro])
        return self._emit(subject, text, self.html_message(intro, items, outro))

    def _mail_resolved(self, resolved):
        n = len(resolved)
        subject = (f"[thermostat] resolved: {resolved[0]['subject']}"
                   if n == 1 else f"[thermostat] {n} issues resolved")
        intro = "The following thermostat issue(s) have cleared:"
        items = [f"{st.get('subject')}: {st.get('detail')} — now OK" for st in resolved]
        text = "\n".join([intro, ""] + [f"  - {it}" for it in items])
        return self._emit(subject, text, self.html_message(intro, items))

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

    def notify(self, subject, body, html_body=None, *, essential=False,
               attach_report=True):
        """Send an arbitrary mail through the unified send path (if enabled).

        Defaults treat it as a budget-counted issue notification that carries the
        full status report. Pass `attach_report=False` when the body already *is*
        the report, and `essential=True` to bypass the daily cap (user-requested
        reports).
        """
        return self._emit(subject, body, html_body, essential=essential,
                           attach_report=attach_report)

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
            msg = "No open thermostat/sensor/heat-pump issues. All good."
            self._emit(f"[thermostat] daily digest {day}: all clear", msg,
                       self.html_message(msg, []), essential=True)
            return
        intro = f"Open thermostat issues as of {day}:"
        items, text_lines = [], [intro, ""]
        for key, st in sorted(open_issues.items()):
            sev = st.get('severity', 'info').upper()
            text = f"{st.get('subject')}: {st.get('detail')}"
            items.append((sev, text))
            text_lines.append(f"  [{sev}] {text}")
        self._emit(f"[thermostat] daily digest {day}: {len(open_issues)} open",
                   "\n".join(text_lines), self.html_message(intro, items),
                   essential=True)
