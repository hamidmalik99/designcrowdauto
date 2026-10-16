"""Step 7: send queued first emails and due follow-ups via Brevo.

Guarantees (all enforced here, not just in Config):
- nothing is sent when AUTO_SEND is FALSE, when paused, in dry-run, or without a business address
- the kill switch is re-read from the sheet before EVERY send
- new emails <= min(DAILY_CAP, 40) and follow-ups <= min(FOLLOWUP_DAILY_CAP, 40) per rolling 24h
- random MIN_GAP..MAX_GAP minutes between any two sends (also across runs)
- only inside the recipient's local SEND_START_HOUR..SEND_END_HOUR on SEND_DAYS
- Suppression re-checked immediately before each send
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import random
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests

import followups
from config import Settings
from store import Store, iso, now_utc, parse_iso

log = logging.getLogger(__name__)

BREVO_SEND_URL = "https://api.brevo.com/v3/smtp/email"


def unsubscribe_token(secret: str, email: str) -> str:
    return hmac.new(secret.encode(), email.strip().lower().encode(), hashlib.sha256).hexdigest()[:32]


def unsubscribe_url(cfg: Settings, email: str) -> str:
    s = cfg.secrets
    return f"{s.unsubscribe_base_url}?{urlencode({'e': email.lower(), 't': unsubscribe_token(s.unsubscribe_secret, email)})}"


def compose(cfg: Settings, row: dict, body: str) -> str:
    """Model-written body + sign-off + legally required footer (added by code)."""
    portfolio = cfg.get("PORTFOLIO_URL").replace("https://", "").replace("http://", "").rstrip("/")
    return (
        f"{body.strip()}\n\n"
        f"Cheers,\n{cfg.get('SENDER_FIRST_NAME')}\n\n"
        f"--\n{cfg.get('SENDER_NAME')} | {cfg.get('SENDER_TITLE')}\n{portfolio}\n{cfg.get('BUSINESS_ADDRESS')}\n"
        f"Don't want these emails? Unsubscribe: {unsubscribe_url(cfg, row['contact_email'])} "
        f"(or reply \"unsubscribe\")"
    )


def in_window(cfg: Settings, tz: str, now: datetime | None = None) -> bool:
    if not tz:
        return False
    try:
        local = (now or now_utc()).astimezone(ZoneInfo(tz))
    except Exception:
        return False
    start, end = cfg.send_hours
    return local.weekday() in cfg.send_days and start <= local.hour < end


def _count_since(store: Store, field: str, since: datetime) -> int:
    return sum(1 for r in store.prospects() if (parse_iso(r.get(field, "")) or since) > since)


def sends_last_24h(store: Store, now: datetime | None = None) -> tuple[int, int]:
    since = (now or now_utc()) - timedelta(hours=24)
    return (_count_since(store, "sent_at", since),
            _count_since(store, "fu1_at", since) + _count_since(store, "fu2_at", since))


def last_send_time(store: Store) -> datetime | None:
    times = [parse_iso(r.get(f, "")) for r in store.prospects() for f in ("sent_at", "fu1_at", "fu2_at")]
    times = [t for t in times if t]
    return max(times) if times else None


def brevo_send(cfg: Settings, row: dict, subject: str, text: str, extra_headers: dict | None = None) -> str:
    s = cfg.secrets
    unsub = unsubscribe_url(cfg, row["contact_email"])
    list_unsub = f"<{unsub}>"
    if s.unsubscribe_mailto:
        list_unsub = f"<mailto:{s.unsubscribe_mailto}?subject=unsubscribe>, " + list_unsub
    headers = {"List-Unsubscribe": list_unsub, "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
    headers.update(extra_headers or {})
    payload = {
        "sender": {"name": cfg.get("SENDER_NAME"), "email": s.sender_email},
        "to": [{"email": row["contact_email"], **({"name": row["contact_name"]} if row.get("contact_name") else {})}],
        "replyTo": {"email": s.reply_to_email or s.sender_email, "name": cfg.get("SENDER_NAME")},
        "subject": subject,
        "textContent": text,
        "headers": headers,
        "tags": ["logo-lead-outreach"],
    }
    r = requests.post(BREVO_SEND_URL, json=payload, timeout=30,
                      headers={"api-key": s.brevo_api_key, "accept": "application/json"})
    if r.status_code >= 300:
        raise RuntimeError(f"Brevo {r.status_code}: {r.text[:200]}")
    return r.json().get("messageId", "")


def eligible(store: Store, cfg: Settings, now: datetime | None = None) -> list[tuple[dict, int]]:
    """(row, kind) pairs that may be sent right now; kind 0 = first email, 1/2 = follow-up."""
    now = now or now_utc()
    new_sent, fu_sent = sends_last_24h(store, now)
    items: list[tuple[dict, int]] = []
    if fu_sent < cfg.followup_cap:
        for row, n in followups.due(store, cfg, now):
            if in_window(cfg, row.get("timezone", ""), now):
                items.append((row, n))
        items = items[: cfg.followup_cap - fu_sent]
    if new_sent < cfg.daily_cap:
        ok_status = {"approved"} if cfg.require_approval else {"approved", "queued"}
        fresh = [r for r in store.prospects()
                 if r.get("status") in ok_status and r.get("email_1_body") and not r.get("sent_at")
                 and in_window(cfg, r.get("timezone", ""), now)]
        items += [(r, 0) for r in fresh[: cfg.daily_cap - new_sent]]
    return items


def send_one(store: Store, cfg: Settings, row: dict, kind: int, sender=brevo_send) -> None:
    if kind == 0:
        subject, body = row["email_1_subject"], row["email_1_body"]
        msg_id = sender(cfg, row, subject, compose(cfg, row, body))
        store.update_prospect(row, status="sent", sent_at=iso(), message_id=msg_id, thread_id=msg_id,
                              last_error="")
    else:
        subject = "Re: " + row["email_1_subject"]
        body = row[f"fu{kind}_body"]
        refs = " ".join(x for x in {row.get("thread_id", ""), row.get("message_id", "")} if x)
        headers = {"In-Reply-To": row.get("message_id", ""), "References": refs} if refs else {}
        msg_id = sender(cfg, row, subject, compose(cfg, row, body), headers)
        store.update_prospect(row, status=f"followup_{kind}", **{f"fu{kind}_at": iso()},
                              message_id=msg_id or row.get("message_id", ""), last_error="")


def run_send(store: Store, cfg_loader, max_runtime_s: int = 3300, sleep=time.sleep, sender=brevo_send,
             reply_checker=None) -> dict:
    """cfg_loader() must return a fresh Settings built from the live Config tab
    (that's how the kill switch takes effect between sends)."""
    stats = {"sent": 0, "followups_sent": 0, "errors": 0, "notes": ""}
    started = time.monotonic()
    gap_s: float | None = None  # random gap chosen once per send
    while True:
        cfg: Settings = cfg_loader()
        items = eligible(store, cfg)
        blockers = cfg.sending_blockers()
        if blockers:
            if items:
                preview = ", ".join(f"{r['business_name']} ({'new' if k == 0 else f'fu{k}'})" for r, k in items[:10])
                stats["notes"] += f"not sending ({'; '.join(blockers)}); would send now: {preview}; "
            else:
                stats["notes"] += f"not sending ({'; '.join(blockers)}); nothing due; "
            break
        if not items:
            break
        row, kind = items[0]

        lo, hi = cfg.gap_minutes
        last = last_send_time(store)
        if last:
            if gap_s is None:
                gap_s = random.uniform(lo * 60, hi * 60)
            wait = (last + timedelta(seconds=gap_s) - now_utc()).total_seconds()
            if wait > 0:
                if time.monotonic() - started + wait > max_runtime_s:
                    stats["notes"] += "stopped for gap/runtime limit; "
                    break
                sleep(wait)
                continue  # re-read config and eligibility after waiting

        if store.is_suppressed(email=row["contact_email"], domain=row.get("domain", "")):
            store.update_prospect(row, status="unsubscribed", skip_reason="on suppression list at send time")
            store.flush()
            continue
        if kind > 0 and reply_checker and reply_checker(row):
            store.update_prospect(row, status="replied", replied_at=iso())
            store.flush()
            continue
        try:
            send_one(store, cfg, row, kind, sender)
            stats["sent" if kind == 0 else "followups_sent"] += 1
            gap_s = None
        except Exception as e:
            log.error("send failed for row %s: %s", row.get("_row"), type(e).__name__)
            store.update_prospect(row, last_error=f"send: {str(e)[:200]}")
            stats["errors"] += 1
            store.flush()
            break  # don't hammer a failing provider
        store.flush()
        if time.monotonic() - started > max_runtime_s:
            break
    return stats
