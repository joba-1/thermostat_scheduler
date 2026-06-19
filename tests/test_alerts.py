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
    a = Alerter(cfg, sender=lambda subj, body: sent.append((subj, body)) or True,
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
