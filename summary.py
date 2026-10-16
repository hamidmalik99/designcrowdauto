"""Step 10: one summary email to Ali each morning, plus ad-hoc alerts."""
from __future__ import annotations

import logging
from collections import Counter
from datetime import timedelta

import requests

from config import Settings
from store import Store, now_utc, parse_iso

log = logging.getLogger(__name__)


def send_plain(cfg: Settings, subject: str, text: str) -> bool:
    s = cfg.secrets
    to = cfg.get("SUMMARY_TO") or s.reply_to_email
    if not (to and s.brevo_api_key and s.sender_email):
        log.info("summary/alert not emailed (SUMMARY_TO / BREVO_API_KEY / SENDER_EMAIL missing):\n%s\n%s", subject, text)
        return False
    r = requests.post("https://api.brevo.com/v3/smtp/email", timeout=30,
                      headers={"api-key": s.brevo_api_key, "accept": "application/json"},
                      json={"sender": {"name": "Outreach bot", "email": s.sender_email},
                            "to": [{"email": to}], "subject": subject, "textContent": text,
                            "tags": ["outreach-internal"]})
    return r.status_code < 300


def build(store: Store, cfg: Settings) -> tuple[str, str]:
    since = now_utc() - timedelta(hours=24)
    logs = [l for l in store.logs() if (parse_iso(str(l.get("run_at", ""))) or since) > since]
    tot = Counter()
    errors_notes = []
    for l in logs:
        for k in ("found", "researched", "skipped", "queued", "sent", "followups_sent", "bounces",
                  "unsubscribes", "errors"):
            try:
                tot[k] += int(l.get(k) or 0)
            except ValueError:
                pass
        if l.get("notes"):
            errors_notes.append(f"[{l.get('step')}] {l['notes']}")

    rows = store.prospects()
    reasons = Counter(r.get("skip_reason") for r in rows
                      if r.get("status") == "skipped" and (parse_iso(r.get("date_found", "")) or since) > since)
    replies = [r for r in rows if (parse_iso(r.get("replied_at", "")) or since) > since]
    queued = sum(1 for r in rows if r.get("status") == "queued")
    approved = sum(1 for r in rows if r.get("status") == "approved")

    lines = [
        "Outreach summary - last 24 hours", "",
        f"New contests found:    {tot['found']}",
        f"Researched:            {tot['researched']}",
        f"Skipped:               {tot['skipped']}",
        f"Drafts written:        {tot['queued']}",
        f"First emails sent:     {tot['sent']}",
        f"Follow-ups sent:       {tot['followups_sent']}",
        f"Replies:               {len(replies)}",
        f"Bounces:               {tot['bounces']}",
        f"Unsubscribes:          {tot['unsubscribes']}",
        f"Errors:                {tot['errors']}", "",
        f"Waiting for your approval: {queued} queued, {approved} approved-not-yet-sent", "",
    ]
    if replies:
        lines.append("REPLIES")
        for r in replies:
            lines.append(f"- {r['business_name']} ({r['contact_email']}) [{r['status']}]: {r.get('reply_thread_url', '')}")
        lines.append("")
    if reasons:
        lines.append("Top skip reasons")
        for reason, n in reasons.most_common(5):
            lines.append(f"- {reason or 'unknown'}: {n}")
        lines.append("")
    state = []
    if cfg.get("PAUSED_REASON"):
        state.append(f"SENDING IS PAUSED: {cfg.get('PAUSED_REASON')}")
    if cfg.dry_run:
        state.append("Dry-run is ON (nothing is being sent).")
    elif not cfg.auto_send:
        state.append("AUTO_SEND is FALSE (nothing is being sent).")
    lines += state + ([""] if state else [])
    if errors_notes:
        lines.append("Run notes")
        lines += [f"- {n}" for n in errors_notes[-15:]]
    subject = f"Outreach: {tot['sent'] + tot['followups_sent']} sent, {len(replies)} replies, {tot['queued']} new drafts"
    return subject, "\n".join(lines)


def run_summary(store: Store, cfg: Settings) -> bool:
    subject, text = build(store, cfg)
    return send_plain(cfg, subject, text)
