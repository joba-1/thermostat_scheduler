#!/usr/bin/env python3
"""
Low-noise operator alerting.

An `Issue` is anything worth telling the operator about. The `Alerter`
deduplicates issues by a stable `key` and, by default, is quiet during the day:
every issue is folded into a per-day log and reported once in the daily report,
which lists everything seen that day — including issues that already cleared
before the report went out.

Only two things break the silence with an immediate mail:
  * service start/stop (`notify_service`), and
  * `critical`-severity issues — extraordinary readings (see the daemon's
    extreme temp/humidity checks). A critical issue mails once when it opens and
    again once when it clears; it is never re-mailed while it persists.

`alert`- and `info`-severity issues never mail on their own; they only shape the
daily report. State (the day's log and per-key notification marks) persists to a
JSON file so a daemon restart does not re-spam.

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

# severity, in order of urgency:
#   'critical' -> mail immediately when it opens and when it clears
#   'alert'    -> daily report only (was: mail immediately); the day's headline items
#   'info'     -> daily report only, low priority
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
        self.digest_hour = cfg.get('digest_hour', 7)
        # Hard ceiling on immediate (critical/service) mails per local day, a
        # backstop against a flapping critical sensor. The daily report is exempt
        # (it always gets through). Anything suppressed still surfaces in the report.
        self.max_mails_per_day = cfg.get('max_mails_per_day', 6)
        # Optional Trac wiki page for the project: appended as a footer link to
        # every mail, so the operator can jump to per-alarm remediation notes.
        self.wiki_url = cfg.get('wiki_url')
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
        if self.wiki_url:
            text = (f"{text}\n\nWhat to do about these alarms: {self.wiki_url}")
            if html_body:
                url = html.escape(self.wiki_url, quote=True)
                html_body = (f"{html_body}<p style=\"margin:14px 0 0;font-size:13px;"
                             f"color:#555\">What to do about these alarms: "
                             f"<a href=\"{url}\">{html.escape(self.wiki_url)}</a></p>")
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

        Folds every observed issue into the day's log (`daily_log`) so the daily
        report can list everything that happened, resolved or not. Mails nothing
        for `alert`/`info` issues. `critical` issues mail immediately: once when
        they open and once when they clear. Returns (mailed, cleared) for logging
        — `mailed` holds the critical issues mailed this pass (usually empty).
        """
        now = self._now()
        day = time.strftime('%Y-%m-%d', time.localtime(now))
        open_state = self.state.setdefault('issues', {})
        seen = {iss.key: iss for iss in issues}

        # per-day log: key -> {severity, subject, detail, first, last, resolved}
        log_day = self.state.setdefault('daily_log', {'day': day, 'issues': {}})
        if log_day.get('day') != day:
            # New local day started before the report hour rolled it over (or the
            # report is disabled): start a fresh log so it never grows unbounded.
            log_day['day'], log_day['issues'] = day, {}
        day_issues = log_day['issues']

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
            rec = day_issues.get(key)
            if rec is None:
                day_issues[key] = {
                    'severity': iss.severity, 'subject': iss.subject,
                    'detail': iss.detail, 'first': now, 'last': now,
                    'resolved': False,
                }
            else:
                rec.update(severity=iss.severity, subject=iss.subject,
                           detail=iss.detail, last=now, resolved=False)

        cleared = [k for k in list(open_state.keys()) if k not in seen]

        mailed = []
        for key, iss in seen.items():
            # A critical issue mails once on open; the mark clears on recovery so
            # a genuine re-occurrence mails again, but a persistent one stays quiet.
            if iss.severity == 'critical' and open_state[key].get('last_alert', 0) == 0:
                if self._mail_critical(iss):
                    open_state[key]['last_alert'] = now
                    mailed.append(iss)

        for k in cleared:
            st = open_state.pop(k, None)
            if k in day_issues:
                day_issues[k]['resolved'] = True
            # Critical recovery: mail once, only if we mailed the opening.
            if st and st.get('severity') == 'critical' and st.get('last_alert'):
                self._mail_critical_resolved(st)

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
                p_up = str(prefix).upper()
                if 'RESOLVED' in p_up:
                    color = '#2e7d32'                       # green: cleared
                elif p_up.startswith(('CRITICAL', 'ALERT')):
                    color = '#b00020'                       # red: needs attention
                else:
                    color = '#777'                          # grey: info
                p.append(f'<li style="margin:3px 0"><span style="color:{color};'
                         f'font-weight:bold">[{esc(str(prefix))}]</span> {esc(text)}</li>')
            else:
                p.append(f'<li style="margin:3px 0">{esc(it)}</li>')
        p.append('</ul>')
        if outro:
            p.append(f'<p style="margin:0;color:#555">{esc(outro)}</p>')
        p.append('</div>')
        return "".join(p)

    def _mail_critical(self, iss):
        subject = f"[thermostat] CRITICAL: {iss.subject}: {iss.detail}"
        intro = "The thermostat manager detected an extraordinary reading:"
        items = [f"{iss.subject}: {iss.detail}"]
        outro = ("You are getting this immediately because it crossed a critical "
                 "threshold. Everything else is collected into the daily report.")
        text = "\n".join([intro, ""] + [f"  - {it}" for it in items] + ["", outro])
        return self._emit(subject, text, self.html_message(intro, items, outro))

    def _mail_critical_resolved(self, st):
        subject = f"[thermostat] resolved (critical): {st.get('subject')}"
        intro = "A critical thermostat reading has returned to normal:"
        items = [f"{st.get('subject')}: {st.get('detail')} — now OK"]
        text = "\n".join([intro, ""] + [f"  - {it}" for it in items])
        return self._emit(subject, text, self.html_message(intro, items))

    def notify_service(self, event, detail=None):
        """Immediate mail on daemon start/stop. `event` is e.g. 'started'/'stopped'."""
        subject = f"[thermostat] service {event}"
        body = f"The thermostat manager {event}." + (f"\n\n{detail}" if detail else "")
        intro = f"The thermostat manager {event}."
        items = [detail] if detail else []
        # Never attach the status report to a shutdown notice — the daemon is on
        # its way down and the snapshot may be empty/stale.
        return self._emit(subject, body, self.html_message(intro, items),
                          attach_report=(event != 'stopped'))

    def maybe_send_digest(self, localtime_fn=time.localtime):
        """Send the daily report once, when the configured hour is reached.

        The report covers everything logged since the last report — issues still
        open and issues that already cleared during the day — then the day's log
        is reset. Kept the name `maybe_send_digest` for the daemon's call site.
        """
        if not self.enabled:
            return False
        lt = localtime_fn(self._now())
        day = time.strftime('%Y-%m-%d', lt)
        if lt.tm_hour < self.digest_hour:
            return False
        if self.state.get('last_digest_day') == day:
            return False
        self.state['last_digest_day'] = day
        log_day = self.state.get('daily_log') or {'issues': {}}
        self._send_report(log_day.get('issues', {}), day)
        # Start a fresh log for the new reporting day (keep currently-open issues
        # so they still show tomorrow if unresolved).
        open_now = self.state.get('issues', {})
        self.state['daily_log'] = {
            'day': day,
            'issues': {k: {'severity': st.get('severity'), 'subject': st.get('subject'),
                           'detail': st.get('detail'), 'first': st.get('first'),
                           'last': self._now(), 'resolved': False}
                       for k, st in open_now.items()},
        }
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

    def notify_rich(self, subject, intro, items=(), outro=None, *,
                    essential=False, attach_report=True):
        """Send a reminder mail built from intro + bulleted items (+ optional
        outro), rendering both a clean HTML body and a matching plain-text
        fallback. Used by the ad-hoc reminders (free cooling, mode change,
        heat-pump alarm) so they read like the rest of the mail instead of a
        monospace block. `items` may be strings or (prefix, text) tuples."""
        items = list(items)

        def _line(it):
            return f"[{it[0]}] {it[1]}" if isinstance(it, tuple) else str(it)

        lines = [intro]
        if items:
            lines += [""] + [f"  - {_line(it)}" for it in items]
        if outro:
            lines += ["", outro]
        return self._emit(subject, "\n".join(lines),
                          self.html_message(intro, items, outro),
                          essential=essential, attach_report=attach_report)

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

    def _send_report(self, day_issues, day):
        if not day_issues:
            # A quiet day: a short "all clear" once a day is reassuring but easy
            # to filter.
            msg = "No thermostat/sensor/heat-pump issues in the last 24h. All good."
            self._emit(f"[thermostat] daily report {day}: all clear", msg,
                       self.html_message(msg, []), essential=True)
            return
        # Still-open issues first, then those that already resolved during the day.
        open_recs = {k: st for k, st in day_issues.items() if not st.get('resolved')}
        resolved_recs = {k: st for k, st in day_issues.items() if st.get('resolved')}
        n_open, n_res = len(open_recs), len(resolved_recs)
        intro = f"Thermostat issues in the last 24h ({n_open} open, {n_res} resolved):"
        items, text_lines = [], [intro, ""]

        def _add(recs, tag):
            for key, st in sorted(recs.items()):
                sev = st.get('severity', 'info').upper()
                label = sev if not tag else f"{sev} · {tag}"
                text = f"{st.get('subject')}: {st.get('detail')}"
                items.append((label, text))
                text_lines.append(f"  [{label}] {text}")

        _add(open_recs, "")
        _add(resolved_recs, "resolved")
        self._emit(f"[thermostat] daily report {day}: {n_open} open, {n_res} resolved",
                   "\n".join(text_lines), self.html_message(intro, items),
                   essential=True)
