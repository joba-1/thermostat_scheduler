import time

from alerts import Alerter, make_issue


def make_alerter(tmp_path, now_box, **extra):
    cfg = {
        'enabled': True,
        'mail_to': 'x@example.com',
        'digest_hour': 7,
        'state_file': str(tmp_path / 'alerts.json'),
        **extra,
    }
    sent = []
    a = Alerter(cfg,
                sender=lambda subj, body, html=None: sent.append((subj, body, html)) or True,
                now_fn=lambda: now_box[0])
    return a, sent


# ---- routine (alert/info) issues: daily report only, no immediate mail -------

def test_alert_issue_never_mails_immediately(tmp_path):
    now_box = [1000.0]
    a, sent = make_alerter(tmp_path, now_box)
    iss = make_issue('d:battery', 'battery_low', 'D thermostat', 'battery low')
    mailed, cleared = a.process([iss])
    assert mailed == [] and sent == []          # routine issues wait for the report


def test_info_severity_never_mails_immediately(tmp_path):
    now_box = [1000.0]
    a, sent = make_alerter(tmp_path, now_box)
    iss = make_issue('r:manual', 'manual_override', 'R thermostat', 'manual', severity='info')
    mailed, cleared = a.process([iss])
    assert mailed == [] and sent == []


def test_cleared_issue_removed_from_state(tmp_path):
    now_box = [1000.0]
    a, sent = make_alerter(tmp_path, now_box)
    iss = make_issue('d:life', 'no_life_sign', 'D thermostat', 'gone')
    a.process([iss])
    mailed, cleared = a.process([])             # issue gone now
    assert cleared == ['d:life']
    assert 'd:life' not in a.state['issues']
    assert sent == []                           # non-critical clear -> no mail


# ---- critical issues: mail on open + on clear --------------------------------

def test_critical_mails_on_open_and_clear(tmp_path):
    now_box = [1000.0]
    a, sent = make_alerter(tmp_path, now_box)
    iss = make_issue('s:tempextreme', 'temperature_extreme', 'S sensor',
                     'temperature 4°C is below 10°C', severity='critical')
    mailed, _ = a.process([iss])
    assert len(sent) == 1 and mailed == [iss]
    assert 'CRITICAL' in sent[0][0]

    a.process([iss])                            # still open -> no re-mail
    assert len(sent) == 1

    a.process([])                               # recovered -> one resolved mail
    assert len(sent) == 2
    assert 'resolved (critical)' in sent[1][0]


def test_critical_reoccurrence_mails_again(tmp_path):
    now_box = [1000.0]
    a, sent = make_alerter(tmp_path, now_box)
    iss = make_issue('s:tempextreme', 'temperature_extreme', 'S', 'hot', severity='critical')
    a.process([iss])
    a.process([])                               # open + clear -> 2 mails
    assert len(sent) == 2
    a.process([iss])                            # comes back -> mails once more
    assert len(sent) == 3


# ---- service start/stop ------------------------------------------------------

def test_service_notifications_mail(tmp_path):
    now_box = [1000.0]
    a, sent = make_alerter(tmp_path, now_box)
    a.notify_service('started', 'Version 9.9')
    a.notify_service('stopped')
    assert len(sent) == 2
    assert 'service started' in sent[0][0]
    assert 'service stopped' in sent[1][0]


# ---- daily report: everything seen that day, resolved or not -----------------

def test_daily_report_includes_resolved(tmp_path):
    now_box = [1000.0]
    a, sent = make_alerter(tmp_path, now_box)
    a.process([make_issue('a:battery', 'battery_low', 'A', 'low'),
               make_issue('b:life', 'no_life_sign', 'B', 'gone')])
    a.process([make_issue('a:battery', 'battery_low', 'A', 'low')])   # B resolved
    assert sent == []                           # nothing mailed during the day

    lt = time.struct_time((2026, 6, 19, 8, 0, 0, 4, 170, -1))
    assert a.maybe_send_digest(localtime_fn=lambda _ts: lt) is True
    subj, body, html = sent[0]
    assert '1 open, 1 resolved' in subj
    assert 'A: low' in body and 'B: gone' in body       # both, open + resolved
    assert 'resolved' in body


def test_daily_report_all_clear_when_quiet(tmp_path):
    now_box = [1000.0]
    a, sent = make_alerter(tmp_path, now_box)
    lt = time.struct_time((2026, 6, 19, 8, 0, 0, 4, 170, -1))
    assert a.maybe_send_digest(localtime_fn=lambda _ts: lt) is True
    assert 'all clear' in sent[0][0]


def test_daily_report_sends_once_per_day(tmp_path):
    now_box = [1000.0]
    a, sent = make_alerter(tmp_path, now_box)
    a.process([make_issue('d:battery', 'battery_low', 'D thermostat', 'low')])
    lt = time.struct_time((2026, 6, 19, 8, 0, 0, 4, 170, -1))
    assert a.maybe_send_digest(localtime_fn=lambda _ts: lt) is True
    assert a.maybe_send_digest(localtime_fn=lambda _ts: lt) is False
    assert len(sent) == 1


def test_report_resets_but_keeps_open_issues(tmp_path):
    now_box = [1000.0]
    a, sent = make_alerter(tmp_path, now_box)
    a.process([make_issue('a:battery', 'battery_low', 'A', 'low')])
    lt = time.struct_time((2026, 6, 19, 8, 0, 0, 4, 170, -1))
    a.maybe_send_digest(localtime_fn=lambda _ts: lt)
    # the still-open issue carries into the next report window
    log = a.state['daily_log']['issues']
    assert 'a:battery' in log and log['a:battery']['resolved'] is False


# ---- backstop cap, persistence, html -----------------------------------------

def test_daily_mail_budget_caps_immediate_mails(tmp_path):
    now_box = [1000.0]
    a, sent = make_alerter(tmp_path, now_box, max_mails_per_day=2)
    for i in range(4):
        iss = make_issue(f's{i}:tempextreme', 'temperature_extreme',
                         f'S{i}', 'hot', severity='critical')
        a.process([iss])
    assert len(sent) == 2                       # capped

    # The daily report is exempt from the cap.
    lt = time.struct_time((2026, 6, 19, 8, 0, 0, 4, 170, -1))
    a.maybe_send_digest(localtime_fn=lambda _ts: lt)
    assert len(sent) == 3


def test_critical_state_persists_across_restart(tmp_path):
    now_box = [1000.0]
    a, sent = make_alerter(tmp_path, now_box)
    iss = make_issue('s:tempextreme', 'temperature_extreme', 'S', 'hot', severity='critical')
    a.process([iss])
    assert len(sent) == 1

    a2, sent2 = make_alerter(tmp_path, now_box)   # same state file, still open
    a2.process([iss])
    assert sent2 == []                            # no re-spam on restart


def test_critical_mail_has_clean_html(tmp_path):
    now_box = [1000.0]
    a, sent = make_alerter(tmp_path, now_box)
    a.process([make_issue('s:tempextreme', 'temperature_extreme', 'S',
                          'temperature 4°C is below 10°C', severity='critical')])
    subj, body, html = sent[0]
    assert html is not None
    assert '<ul' in html and '<pre' not in html
    assert '4°C' in html


def test_due_fires_once_per_interval(tmp_path):
    now_box = [1_000_000.0]
    a, _ = make_alerter(tmp_path, now_box)
    assert a.due('status_report', 3600) is True
    assert a.due('status_report', 3600) is False
    now_box[0] += 3601
    assert a.due('status_report', 3600) is True


def test_wiki_url_appended_to_every_mail(tmp_path):
    now_box = [1000.0]
    a, sent = make_alerter(tmp_path, now_box,
                           wiki_url='http://trac/wiki/ThermostatScheduler#Alarms')
    a.process([make_issue('s:tx', 'temperature_extreme', 'S', 'hot', severity='critical')])
    subj, body, html = sent[0]
    assert 'http://trac/wiki/ThermostatScheduler#Alarms' in body
    assert 'href="http://trac/wiki/ThermostatScheduler#Alarms"' in html
    # also on the daily report
    lt = time.struct_time((2026, 6, 19, 8, 0, 0, 4, 170, -1))
    a.maybe_send_digest(localtime_fn=lambda _ts: lt)
    assert 'ThermostatScheduler#Alarms' in sent[-1][1]


def test_no_wiki_footer_when_unset(tmp_path):
    now_box = [1000.0]
    a, sent = make_alerter(tmp_path, now_box)
    a.notify_service('started')
    assert 'What to do about these alarms' not in sent[0][1]


def test_report_provider_appended_to_critical_mail(tmp_path):
    now_box = [1000.0]
    a, sent = make_alerter(tmp_path, now_box)
    a.report_provider = lambda: ("PLAINREPORT", "<b>HTMLREPORT</b>")
    a.process([make_issue('s:tempextreme', 'temperature_extreme', 'S', 'hot',
                          severity='critical')])
    subj, body, html = sent[0]
    assert 'PLAINREPORT' in body
    assert 'HTMLREPORT' in html
