"""Step 8: replies (Gmail, read-only), bounces/unsubscribes/complaints (Brevo),
Suppression sync, and the bounce/complaint auto-pause."""
from __future__ import annotations

import logging
import re
from datetime import timedelta

import requests

from config import Settings
from research import FREE_MAIL
from store import ACTIVE_STATUSES, Store, iso, normalize_domain, now_utc, parse_iso

log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
OPT_OUT_RE = re.compile(r"\b(unsubscribe|remove me|remove us|take me off|not interested|no thanks|"
                        r"stop (emailing|contacting|sending)|^stop\b|don't contact|do not contact)", re.I | re.M)
AUTO_REPLY_RE = re.compile(r"out of (the )?office|auto(matic)?[- ]?reply|autoreply|away from (the )?office|"
                           r"on leave|delivery status notification|undeliverable|mail delivery", re.I)
BOUNCE_RATE_LIMIT = 0.05
BOUNCE_MIN_SAMPLE = 10
BOUNCE_ABS_LIMIT = 3


# --- Gmail -------------------------------------------------------------------
def gmail_service(cfg: Settings):
    s = cfg.secrets
    if not (s.gmail_client_id and s.gmail_client_secret and s.gmail_refresh_token):
        return None
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(None, refresh_token=s.gmail_refresh_token, client_id=s.gmail_client_id,
                        client_secret=s.gmail_client_secret, token_uri="https://oauth2.googleapis.com/token",
                        scopes=GMAIL_SCOPES)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def find_reply(service, row: dict) -> dict | None:
    """First non-automatic message from the prospect's domain after our first send."""
    sent = parse_iso(row.get("sent_at", ""))
    domain = row.get("domain") or normalize_domain(row.get("contact_email", "").split("@")[-1])
    if not sent or not domain:
        return None
    # Free-mail contacts: match the exact address; otherwise anyone at their domain counts.
    sender = row["contact_email"] if row["contact_email"].split("@")[-1] in FREE_MAIL else domain
    q = f"from:{sender} after:{int(sent.timestamp())} -in:sent -in:chats"
    res = service.users().messages().list(userId="me", q=q, maxResults=5).execute()
    for m in res.get("messages", []):
        msg = service.users().messages().get(userId="me", id=m["id"], format="metadata",
                                             metadataHeaders=["Subject", "From"]).execute()
        hdrs = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
        subject = hdrs.get("subject", "")
        if AUTO_REPLY_RE.search(subject):
            continue
        return {"thread_id": msg.get("threadId", ""), "subject": subject, "snippet": msg.get("snippet", ""),
                "url": f"https://mail.google.com/mail/u/0/#all/{msg.get('threadId', '')}"}
    return None


def check_replies(store: Store, cfg: Settings, service=None) -> dict:
    stats = {"replies": 0, "unsubscribes": 0, "reply_links": []}
    service = service or gmail_service(cfg)
    if service is None:
        stats["note"] = "Gmail not configured - reply detection skipped"
        return stats
    for row in store.prospects():
        if row.get("status") not in ACTIVE_STATUSES:
            continue
        try:
            reply = find_reply(service, row)
        except Exception as e:
            log.warning("gmail check failed for row %s: %s", row.get("_row"), type(e).__name__)
            continue
        if not reply:
            continue
        if OPT_OUT_RE.search(reply["subject"] + "\n" + reply["snippet"]):
            store.suppress(row["contact_email"], "email", "opt-out reply", "gmail")
            store.suppress(row["domain"], "domain", "opt-out reply", "gmail")
            store.update_prospect(row, status="unsubscribed", replied_at=iso(), reply_thread_url=reply["url"])
            stats["unsubscribes"] += 1
        else:
            store.update_prospect(row, status="replied", replied_at=iso(), reply_thread_url=reply["url"])
            stats["replies"] += 1
            stats["reply_links"].append(f"{row['business_name']}: {reply['url']}")
    # Unsubscribe-by-email: messages sent to the List-Unsubscribe mailto address.
    if cfg.secrets.unsubscribe_mailto:
        try:
            res = service.users().messages().list(
                userId="me", q=f"to:{cfg.secrets.unsubscribe_mailto} newer_than:14d", maxResults=50).execute()
            for m in res.get("messages", []):
                msg = service.users().messages().get(userId="me", id=m["id"], format="metadata",
                                                     metadataHeaders=["From"]).execute()
                frm = next((h["value"] for h in msg.get("payload", {}).get("headers", [])
                            if h["name"].lower() == "from"), "")
                addr = re.search(r"[\w.+-]+@[\w.-]+", frm)
                if addr and not store.is_suppressed(email=addr.group(0)):
                    store.suppress(addr.group(0), "email", "unsubscribe email", "list-unsubscribe mailto")
                    stats["unsubscribes"] += 1
        except Exception as e:
            log.warning("unsubscribe mailbox check failed: %s", type(e).__name__)
    store.flush()
    return stats


def has_reply(cfg: Settings, service=None):
    """Return a callable(row)->bool for send.py's last-second follow-up check."""
    service = service or gmail_service(cfg)
    if service is None:
        return None

    def check(row: dict) -> bool:
        try:
            return find_reply(service, row) is not None
        except Exception:
            return True  # unsure -> don't send the follow-up this run
    return check


# --- Brevo -------------------------------------------------------------------
def brevo_events(cfg: Settings, event: str, days: int = 2) -> list[dict]:
    if not cfg.secrets.brevo_api_key:
        return []
    r = requests.get("https://api.brevo.com/v3/smtp/statistics/events",
                     params={"event": event, "days": days, "limit": 2500, "tags": "logo-lead-outreach"},
                     headers={"api-key": cfg.secrets.brevo_api_key, "accept": "application/json"}, timeout=30)
    r.raise_for_status()
    return r.json().get("events", []) or []


def process_brevo(store: Store, cfg: Settings, fetch=brevo_events) -> dict:
    stats = {"bounces": 0, "unsubscribes": 0, "complaints": 0, "hard_bounces_24h": 0}
    by_email = {r["contact_email"].lower(): r for r in store.prospects() if r.get("contact_email")}
    since = now_utc() - timedelta(hours=24)
    for event, status, reason in [("hardBounces", "bounced", "hard bounce"), ("invalid", "bounced", "invalid address"),
                                  ("blocked", "bounced", "blocked by Brevo"),
                                  ("unsubscribed", "unsubscribed", "unsubscribed"),
                                  ("spam", "unsubscribed", "spam complaint")]:
        try:
            events = fetch(cfg, event)
        except requests.RequestException as e:
            log.warning("brevo %s fetch failed: %s", event, type(e).__name__)
            continue
        for ev in events:
            email = (ev.get("email") or "").lower()
            if not email:
                continue
            when = parse_iso(ev.get("date", ""))
            if event == "hardBounces" and when and when > since:
                stats["hard_bounces_24h"] += 1
            if event == "spam" and when and when > since:
                stats["complaints"] += 1
            store.suppress(email, "email", reason, "brevo")
            row = by_email.get(email)
            if row and row.get("status") not in {"bounced", "unsubscribed"}:
                store.update_prospect(row, status=status, skip_reason=reason)
                stats["bounces" if status == "bounced" else "unsubscribes"] += 1
    store.flush()
    return stats


def health_check(store: Store, cfg: Settings, brevo_stats: dict, alert=None) -> str:
    """Auto-pause on bounce rate > 5% (or 3+ hard bounces) in 24h, or any complaint."""
    since = now_utc() - timedelta(hours=24)
    sends = sum(1 for r in store.prospects() for f in ("sent_at", "fu1_at", "fu2_at")
                if (parse_iso(r.get(f, "")) or since) > since)
    hb, spam = brevo_stats.get("hard_bounces_24h", 0), brevo_stats.get("complaints", 0)
    reason = ""
    if spam:
        reason = f"{spam} spam complaint(s) in 24h"
    elif hb >= BOUNCE_ABS_LIMIT or (sends >= BOUNCE_MIN_SAMPLE and hb / sends > BOUNCE_RATE_LIMIT):
        reason = f"bounce rate {hb}/{sends} in 24h"
    if reason and not cfg.get("PAUSED_REASON"):
        store.set_config("AUTO_SEND", "FALSE")
        store.set_config("PAUSED_REASON", f"{iso()}: {reason}")
        if alert:
            alert(f"Outreach auto-paused: {reason}",
                  f"Sending was paused automatically: {reason}.\n\nCheck the sheet, then clear PAUSED_REASON "
                  f"and set AUTO_SEND to TRUE to resume.")
    return reason


def sync_suppression(store: Store) -> int:
    """Any not-yet-finished row whose email/domain got suppressed (e.g. via the
    unsubscribe link, which writes straight into the Suppression tab) is stopped."""
    n = 0
    for row in store.prospects():
        if row.get("status") in ACTIVE_STATUSES | {"queued", "approved", "researched", "new"} and \
                (row.get("contact_email") or row.get("domain")) and \
                store.is_suppressed(email=row.get("contact_email", ""), domain=row.get("domain", "")):
            store.update_prospect(row, status="unsubscribed", skip_reason="on suppression list")
            n += 1
    store.flush()
    return n


def run_inbox(store: Store, cfg: Settings, alert=None) -> dict:
    stats = check_replies(store, cfg)
    try:
        b = process_brevo(store, cfg)
    except Exception as e:
        log.warning("brevo processing failed: %s", type(e).__name__)
        b = {}
    stats["bounces"] = b.get("bounces", 0)
    stats["unsubscribes"] += b.get("unsubscribes", 0) + sync_suppression(store)
    stats["paused"] = health_check(store, cfg, b, alert)
    return stats
