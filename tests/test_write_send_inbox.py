import hashlib
import hmac
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import followups
import inbox
import send
import write
from config import HARD_DAILY_CAP, Settings
from conftest import Clock
from store import iso

GOOD_BODY = ("Hi Jane,\n\n" + " ".join(["word"] * 120) + "\n\nWant me to send a free 3-point audit of your site?")


def fake_gen(outputs):
    calls = []

    def gen(prompt):
        calls.append(prompt)
        return outputs[min(len(calls) - 1, len(outputs) - 1)]
    gen.calls = calls
    return gen


# --- write -------------------------------------------------------------------
def test_validate_catches_rule_breaks():
    assert write.validate("quick idea for harbour plumbing website", GOOD_BODY) == []
    bad = GOOD_BODY + " I saw your DesignCrowd contest for a new logo. Visit https://a.com and https://b.com"
    problems = " ".join(write.validate("quick idea", bad))
    for p in ("designcrowd", "contest", "logo", "more than one link"):
        assert p in problems
    assert any("word count" in p for p in write.validate("x", GOOD_BODY + " extra" * 40))
    assert any("placeholder" in p for p in write.validate("idea for {business}", GOOD_BODY))
    assert any("hype" in p for p in write.validate("x", GOOD_BODY.replace("word word", "skyrocket", 1)))


def test_generate_regenerates_once_then_gives_up(live_cfg):
    gen = fake_gen([{"subject": "s", "body": "too short"}, {"subject": "quick idea", "body": GOOD_BODY}])
    email, problems = write.generate(live_cfg, "p", 0, gen)
    assert email and len(gen.calls) == 2 and "failed these checks" in gen.calls[1]
    gen = fake_gen([{"subject": "s", "body": "too short"}])
    email, problems = write.generate(live_cfg, "p", 0, gen)
    assert email is None and len(gen.calls) == 2


def test_prompt_never_contains_brief_source(store, live_cfg):
    row = store.add_prospect({"business_name": "Harbour Plumbing Co", "status": "researched",
                              "source_url": "https://www.designcrowd.com/design/123", "contact_email": "a@b.com"})
    gen = fake_gen([{"subject": "quick idea for harbour plumbing", "body": GOOD_BODY}])
    stats = write.run_write(store, live_cfg, gen)
    assert stats["queued"] == 1 and row["status"] == "queued"
    assert "designcrowd" not in gen.calls[0].lower()


def test_run_write_marks_validation_failures_skipped(store, live_cfg):
    row = store.add_prospect({"business_name": "X", "status": "researched"})
    write.run_write(store, live_cfg, fake_gen([{"subject": "", "body": "nope"}]))
    assert row["status"] == "skipped" and row["skip_reason"] == "copy failed validation"


# --- send --------------------------------------------------------------------
def queued_row(store, n=0, **kw):
    data = {"business_name": f"Biz {n}", "contact_email": f"info@biz{n}.com.au", "domain": f"biz{n}.com.au",
            "timezone": "Australia/Sydney", "status": "queued", "email_1_subject": "quick idea for biz",
            "email_1_body": GOOD_BODY}
    data.update(kw)
    return store.add_prospect(data)


def test_unsubscribe_token_matches_apps_script_algorithm(live_cfg):
    url = send.unsubscribe_url(live_cfg, "Info@Biz.com")
    q = parse_qs(urlparse(url).query)
    expected = hmac.new(b"s3cret", b"info@biz.com", hashlib.sha256).hexdigest()[:32]
    assert q["e"] == ["info@biz.com"] and q["t"] == [expected]


def test_footer_has_identity_address_and_unsubscribe(store, live_cfg):
    row = queued_row(store)
    text = send.compose(live_cfg, row, row["email_1_body"])
    assert "Hamid Ali Asad" in text and "1 Test St, Lahore" in text and "Unsubscribe: https://" in text


def test_sending_window(secrets, tuesday_10am_sydney):
    cfg = Settings({}, secrets)  # defaults: Tue-Thu 9-16
    assert send.in_window(cfg, "Australia/Sydney", tuesday_10am_sydney)
    assert not send.in_window(cfg, "Europe/London", tuesday_10am_sydney)       # Mon 11pm in London
    assert not send.in_window(cfg, "Australia/Sydney", tuesday_10am_sydney + timedelta(hours=6))  # 4pm
    assert not send.in_window(cfg, "", tuesday_10am_sydney)


def test_daily_cap_never_exceeds_hard_cap(secrets):
    assert Settings({"DAILY_CAP": "500"}, secrets).daily_cap == HARD_DAILY_CAP


def test_nothing_sent_in_dry_run_or_kill_switch(store, secrets):
    queued_row(store)
    calls = []
    for cfg_vals in ({"AUTO_SEND": "TRUE", "DRY_RUN": "TRUE", "BUSINESS_ADDRESS": "x"},
                     {"AUTO_SEND": "FALSE", "DRY_RUN": "FALSE", "BUSINESS_ADDRESS": "x"},
                     {"AUTO_SEND": "TRUE", "DRY_RUN": "FALSE", "BUSINESS_ADDRESS": ""}):
        vals = dict(cfg_vals, REQUIRE_APPROVAL="FALSE", SEND_DAYS="Mon,Tue,Wed,Thu,Fri,Sat,Sun",
                    SEND_START_HOUR="0", SEND_END_HOUR="24")
        stats = send.run_send(store, lambda: Settings(vals, secrets), sender=lambda *a: calls.append(a))
        assert stats["sent"] == 0 and "not sending" in stats["notes"]
    assert calls == []


def test_require_approval_only_sends_approved(store, live_cfg, secrets):
    queued_row(store, 1)
    approved = queued_row(store, 2, status="approved")
    cfg = Settings(dict(live_cfg.raw, REQUIRE_APPROVAL="TRUE"), secrets)
    assert [r for r, _ in send.eligible(store, cfg)] == [approved]


def test_send_loop_caps_gaps_and_kill_switch(store, live_cfg, secrets, monkeypatch, tuesday_10am_sydney):
    clock = Clock(tuesday_10am_sydney)
    monkeypatch.setattr(send, "now_utc", clock)
    monkeypatch.setattr("store.now_utc", clock)
    for i in range(5):
        queued_row(store, i)
    cfg_vals = dict(live_cfg.raw, DAILY_CAP="3")
    sent_times = []

    def sender(cfg, row, subject, text, headers=None):
        sent_times.append(clock.now)
        return f"<m{len(sent_times)}@brevo>"

    stats = send.run_send(store, lambda: Settings(cfg_vals, secrets), sleep=clock.sleep, sender=sender,
                          max_runtime_s=10**9)
    assert stats["sent"] == 3  # daily cap
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(sent_times, sent_times[1:])]
    assert all(4 <= g <= 9 for g in gaps), gaps

    # kill switch flipped in the sheet after the first send of a new batch stops further sends
    for i in range(5, 8):
        queued_row(store, i)
    clock.sleep(25 * 3600)
    state = {"n": 0}

    def loader():
        state["n"] += 1
        return Settings(dict(cfg_vals, AUTO_SEND="TRUE" if state["n"] == 1 else "FALSE"), secrets)

    stats = send.run_send(store, loader, sleep=clock.sleep, sender=sender, max_runtime_s=10**9)
    assert stats["sent"] == 1


def test_suppression_rechecked_right_before_send(store, live_cfg):
    row = queued_row(store)
    store.suppress("biz0.com.au", "domain", "unsubscribe link", "test")
    calls = []
    send.run_send(store, lambda: live_cfg, sender=lambda *a: calls.append(a))
    assert calls == [] and row["status"] == "unsubscribed"


def test_followups_thread_and_stop_on_reply(store, live_cfg, monkeypatch):
    now = send.now_utc()
    row = queued_row(store, status="sent", sent_at=iso(now - timedelta(days=5)),
                     message_id="<m1@brevo>", thread_id="<m1@brevo>", fu1_body="Hi Jane, " + "word " * 45)
    replied = queued_row(store, 9, status="sent", sent_at=iso(now - timedelta(days=5)), message_id="<m9@brevo>",
                         fu1_body="Hi, " + "word " * 45, replied_at=iso(now))
    captured = []

    def sender(cfg, r, subject, text, headers=None):
        captured.append((r, subject, headers))
        return "<m2@brevo>"

    send.run_send(store, lambda: live_cfg, sender=sender, sleep=lambda s: None, max_runtime_s=0)
    assert len(captured) == 1 and captured[0][0] is row
    _, subject, headers = captured[0]
    assert subject.startswith("Re: ") and headers["In-Reply-To"] == "<m1@brevo>"
    assert row["status"] == "followup_1" and row["fu1_at"]
    assert replied["status"] == "sent" and not replied["fu1_at"]


def test_followup_prepare_and_close_out(store, live_cfg):
    now = send.now_utc()
    row = queued_row(store, status="sent", sent_at=iso(now - timedelta(days=4)))
    gen = fake_gen([{"subject": "", "body": "Hi Jane, " + "word " * 50}])
    assert followups.prepare(store, live_cfg, gen)["drafted"] == 1 and row["fu1_body"]
    old = queued_row(store, 3, status="followup_2", fu2_at=iso(now - timedelta(days=8)),
                     sent_at=iso(now - timedelta(days=20)))
    assert followups.close_out(store, live_cfg) == 1 and old["status"] == "done"


# --- inbox -------------------------------------------------------------------
def test_brevo_bounce_and_unsubscribe_feed_suppression(store, live_cfg):
    b = queued_row(store, 1, status="sent", sent_at=iso())
    u = queued_row(store, 2, status="followup_1", sent_at=iso())
    events = {"hardBounces": [{"email": "info@biz1.com.au", "date": iso()}],
              "unsubscribed": [{"email": "info@biz2.com.au", "date": iso()}]}
    stats = inbox.process_brevo(store, live_cfg, fetch=lambda cfg, ev: events.get(ev, []))
    assert b["status"] == "bounced" and u["status"] == "unsubscribed"
    assert store.is_suppressed(email="info@biz1.com.au") and store.is_suppressed(email="info@biz2.com.au")
    assert stats["hard_bounces_24h"] == 1


def test_health_check_pauses_on_complaint_or_bounces(store, live_cfg):
    alerts = []
    reason = inbox.health_check(store, live_cfg, {"complaints": 1}, lambda s, b: alerts.append(s))
    assert "complaint" in reason and store.config()["AUTO_SEND"] == "FALSE" and alerts
    assert not Settings(store.config(), live_cfg.secrets).auto_send


class FakeGmail:
    def __init__(self, msgs):
        self.msgs = msgs

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, q, maxResults):
        self._q = q
        hits = [m for m in self.msgs if m["from_domain"] in q]
        return type("R", (), {"execute": lambda s: {"messages": [{"id": m["id"]} for m in hits]}})()

    def get(self, userId, id, format, metadataHeaders):
        m = next(x for x in self.msgs if x["id"] == id)
        payload = {"threadId": "t" + id, "snippet": m["snippet"],
                   "payload": {"headers": [{"name": "Subject", "value": m["subject"]}]}}
        return type("R", (), {"execute": lambda s: payload})()


def test_replies_stop_sequence_and_optouts_suppress(store, live_cfg):
    r1 = queued_row(store, 1, status="sent", sent_at=iso())
    r2 = queued_row(store, 2, status="followup_1", sent_at=iso())
    r3 = queued_row(store, 3, status="sent", sent_at=iso())
    gmail = FakeGmail([
        {"id": "1", "from_domain": "biz1.com.au", "subject": "Re: quick idea", "snippet": "Sure, send the audit"},
        {"id": "2", "from_domain": "biz2.com.au", "subject": "Re: quick idea", "snippet": "Not interested, thanks"},
        {"id": "3", "from_domain": "biz3.com.au", "subject": "Out of Office", "snippet": "Back Monday"},
    ])
    stats = inbox.check_replies(store, live_cfg, service=gmail)
    assert r1["status"] == "replied" and "mail.google.com" in r1["reply_thread_url"]
    assert r2["status"] == "unsubscribed" and store.is_suppressed(domain="biz2.com.au")
    assert r3["status"] == "sent"  # auto-reply ignored
    assert stats["replies"] == 1 and stats["unsubscribes"] == 1


def test_sync_suppression_stops_queued_rows(store):
    row = queued_row(store)
    store.suppress("info@biz0.com.au", "email", "unsubscribe link", "apps script")
    assert inbox.sync_suppression(store) == 1 and row["status"] == "unsubscribed"
