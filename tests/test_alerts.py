import os

from alerts import Alerter, make_issue


def make_alerter(tmp_path, now_box, cooldown_hours=24):
    cfg = {
        'enabled': True,
        'mail_to': 'x@example.com',
        'cooldown_hours': cooldown_hours,
        'digest_hour': 7,
        'state_file': str(tmp_path / 'alerts.json'),
    }
    sent = []
    a = Alerter(cfg,
                sender=lambda subj, body, html=None: sent.append((subj, body, html)) or True,
                now_fn=lambda: now_box[0])
    return a, sent


def test_new_alert_mails_once_then_throttled(tmp_path):
    now_box = [1000.0]
    a, sent = make_alerter(tmp_path, now_box)
    iss = make_issue('d:battery', 'battery_low', 'D thermostat', 'battery low')

    a.process([iss])
    assert len(sent) == 1                      # first sighting -> mail

    now_box[0] += 3600                          # 1h later, still open
    a.process([iss])
    assert len(sent) == 1                      # within cooldown -> no new mail

    now_box[0] += 24 * 3600                     # past cooldown
    a.process([iss])
    assert len(sent) == 2                      # re-alert


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


def test_resolved_alert_sends_recovery_mail(tmp_path):
    now_box = [1000.0]
    a, sent = make_alerter(tmp_path, now_box)
    iss = make_issue('d:battery', 'battery_low', 'D thermostat', 'battery 13%')
    a.process([iss])
    assert len(sent) == 1                       # initial alert
    now_box[0] += 11 * 60                        # past the batch window
    a.process([])                               # battery swapped -> cleared
    assert len(sent) == 2
    assert sent[1][0].startswith('[thermostat] resolved:')


def test_resolved_not_sent_for_info_only(tmp_path):
    now_box = [1000.0]
    a, sent = make_alerter(tmp_path, now_box)
    iss = make_issue('r:manual', 'manual_override', 'R thermostat', 'manual', severity='info')
    a.process([iss])
    a.process([])                               # cleared, but never alerted
    assert sent == []


def test_alert_mail_has_clean_html(tmp_path):
    now_box = [1000.0]
    a, sent = make_alerter(tmp_path, now_box)
    a.process([make_issue('d:battery', 'battery_low', 'D thermostat', 'battery 13%')])
    subj, body, html = sent[0]
    assert html is not None
    # proportional font + list, not the monospace <pre> block
    assert '<ul' in html and '<pre' not in html
    assert 'battery 13%' in html


def test_state_persists_across_restart(tmp_path):
    now_box = [1000.0]
    a, sent = make_alerter(tmp_path, now_box)
    iss = make_issue('d:battery', 'battery_low', 'D thermostat', 'battery low')
    a.process([iss])
    assert len(sent) == 1

    # New Alerter instance, same state file, within cooldown -> no re-spam
    a2, sent2 = make_alerter(tmp_path, now_box)
    a2.process([iss])
    assert sent2 == []


def test_due_fires_once_per_interval(tmp_path):
    now_box = [1_000_000.0]
    a, _ = make_alerter(tmp_path, now_box)
    assert a.due('status_report', 3600) is True     # first call (never fired)
    assert a.due('status_report', 3600) is False     # within interval
    now_box[0] += 3601
    assert a.due('status_report', 3600) is True      # interval elapsed


def test_daily_digest_sends_once_per_day(tmp_path):
    import time
    now_box = [1000.0]
    a, sent = make_alerter(tmp_path, now_box)
    a.process([make_issue('d:battery', 'battery_low', 'D thermostat', 'battery low')])
    sent.clear()

    # 08:00 local -> digest fires once
    lt = time.struct_time((2026, 6, 19, 8, 0, 0, 4, 170, -1))
    assert a.maybe_send_digest(localtime_fn=lambda _ts: lt) is True
    assert a.maybe_send_digest(localtime_fn=lambda _ts: lt) is False
    assert len(sent) == 1
    assert 'open' in sent[0][0]


def _budget_alerter(tmp_path, now_box, cap):
    cfg = {
        'enabled': True, 'mail_to': 'x@example.com',
        'max_mails_per_day': cap,
        'batch_window_minutes': 0,      # flush every pass, to isolate the cap
        'cooldown_hours': 999,          # no re-alert noise during the test
        'state_file': str(tmp_path / 'alerts.json'),
    }
    sent = []
    a = Alerter(cfg,
                sender=lambda subj, body, html=None: sent.append((subj, body, html)) or True,
                now_fn=lambda: now_box[0])
    return a, sent


def test_daily_mail_budget_caps_issue_mails(tmp_path):
    now_box = [1000.0]
    a, sent = _budget_alerter(tmp_path, now_box, cap=3)
    # Distinct issues appearing one at a time (kept open) -> one mail each, but
    # only 3 get through before the daily cap suppresses the rest.
    open_now = []
    for i in range(5):
        open_now.append(make_issue(f'd{i}:battery', 'battery_low', f'T{i}', 'low'))
        a.process(list(open_now))
    assert len(sent) == 3                          # capped at the daily budget

    # The daily digest is exempt from the cap and still goes out.
    import time
    lt = time.struct_time((2026, 6, 19, 8, 0, 0, 4, 170, -1))
    assert a.maybe_send_digest(localtime_fn=lambda _ts: lt) is True
    assert len(sent) == 4


def test_budget_resets_next_day(tmp_path):
    now_box = [1000.0]
    a, sent = _budget_alerter(tmp_path, now_box, cap=1)
    a.process([make_issue('a:battery', 'battery_low', 'A', 'low')])
    a.process([make_issue('a:battery', 'battery_low', 'A', 'low'),
               make_issue('b:battery', 'battery_low', 'B', 'low')])
    assert len(sent) == 1                           # second is over budget
    now_box[0] += 24 * 3600                          # next day -> budget resets
    a.process([make_issue('a:battery', 'battery_low', 'A', 'low'),
               make_issue('b:battery', 'battery_low', 'B', 'low'),
               make_issue('c:battery', 'battery_low', 'C', 'low')])
    assert len(sent) == 2


def test_batch_window_coalesces_burst_into_one_mail(tmp_path):
    now_box = [1000.0]
    cfg = {
        'enabled': True, 'mail_to': 'x@example.com',
        'batch_window_minutes': 10,
        'state_file': str(tmp_path / 'alerts.json'),
    }
    sent = []
    a = Alerter(cfg,
                sender=lambda subj, body, html=None: sent.append((subj, body, html)) or True,
                now_fn=lambda: now_box[0])
    # First issue flushes promptly (window starts empty).
    a.process([make_issue('a:life', 'no_life_sign', 'A', 'gone')])
    assert len(sent) == 1
    # More issues arrive over the next few passes, still inside the window:
    # collected, not mailed.
    for dt in (60, 120, 180):
        now_box[0] += dt
        a.process([make_issue('a:life', 'no_life_sign', 'A', 'gone'),
                   make_issue('b:life', 'no_life_sign', 'B', 'gone'),
                   make_issue('c:life', 'no_life_sign', 'C', 'gone')])
    assert len(sent) == 1                            # nothing extra mid-window
    # Once the window elapses, the backlog goes out in one combined mail.
    now_box[0] += 11 * 60
    a.process([make_issue('a:life', 'no_life_sign', 'A', 'gone'),
               make_issue('b:life', 'no_life_sign', 'B', 'gone'),
               make_issue('c:life', 'no_life_sign', 'C', 'gone')])
    assert len(sent) == 2
    assert '2 new alerts' in sent[1][0]             # B and C batched together


def test_transient_flap_within_window_never_mails(tmp_path):
    now_box = [1000.0]
    cfg = {
        'enabled': True, 'mail_to': 'x@example.com',
        'batch_window_minutes': 10,
        'state_file': str(tmp_path / 'alerts.json'),
    }
    sent = []
    a = Alerter(cfg,
                sender=lambda subj, body, html=None: sent.append((subj, body, html)) or True,
                now_fn=lambda: now_box[0])
    # Use up the prompt first-flush on an unrelated issue so the window is armed.
    a.process([make_issue('keep:life', 'no_life_sign', 'K', 'gone')])
    assert len(sent) == 1
    # A sensor flaps: appears then clears, all inside the same window.
    now_box[0] += 60
    a.process([make_issue('keep:life', 'no_life_sign', 'K', 'gone'),
               make_issue('flap:life', 'no_life_sign', 'F', 'gone')])
    now_box[0] += 60
    a.process([make_issue('keep:life', 'no_life_sign', 'K', 'gone')])  # flap gone
    now_box[0] += 11 * 60
    a.process([make_issue('keep:life', 'no_life_sign', 'K', 'gone')])
    assert len(sent) == 1                            # flap left no alert, no recovery


def test_report_provider_appended_to_mail(tmp_path):
    now_box = [1000.0]
    a, sent = make_alerter(tmp_path, now_box)
    a.report_provider = lambda: ("PLAINREPORT", "<b>HTMLREPORT</b>")
    a.process([make_issue('d:battery', 'battery_low', 'D', 'low')])
    subj, body, html = sent[0]
    assert 'PLAINREPORT' in body                     # full status report attached
    assert 'HTMLREPORT' in html
