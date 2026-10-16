"""Step 9: follow-ups. At most 2, same thread, never after a reply/bounce/unsubscribe.

prepare() drafts follow-up copy a day ahead (so it shows in the sheet for review);
due() tells send.py which drafted follow-ups are due now; close_out() marks
finished sequences as done.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from config import Settings
from store import Store, now_utc, parse_iso

log = logging.getLogger(__name__)

STAGE = {1: ("sent", "fu1_body", "fu1_at"), 2: ("followup_1", "fu2_body", "fu2_at")}


def _days(cfg: Settings, n: int) -> int:
    return cfg.followup_1_days if n == 1 else cfg.followup_2_days


def due(store: Store, cfg: Settings, now: datetime | None = None) -> list[tuple[dict, int]]:
    now = now or now_utc()
    out = []
    for row in store.prospects():
        sent = parse_iso(row.get("sent_at", ""))
        if not sent or row.get("replied_at"):
            continue
        for n, (status, body_col, at_col) in STAGE.items():
            if row.get("status") == status and row.get(body_col) and not row.get(at_col) \
                    and now - sent >= timedelta(days=_days(cfg, n)):
                out.append((row, n))
    return out


def prepare(store: Store, cfg: Settings, generator=None, lookahead_days: int = 1) -> dict:
    import write

    stats = {"drafted": 0, "errors": 0}
    horizon = now_utc() + timedelta(days=lookahead_days)
    for row in store.prospects():
        sent = parse_iso(row.get("sent_at", ""))
        if not sent or row.get("replied_at"):
            continue
        for n, (status, body_col, _) in STAGE.items():
            if row.get("status") != status or row.get(body_col):
                continue
            if horizon - sent < timedelta(days=_days(cfg, n)):
                continue
            try:
                email, problems = write.write_followup(cfg, row, n, generator)
            except Exception as e:
                log.exception("follow-up draft failed for row %s", row.get("_row"))
                store.update_prospect(row, last_error=f"followup{n}: {type(e).__name__}")
                stats["errors"] += 1
                continue
            if email:
                store.update_prospect(row, **{body_col: email["body"]})
                stats["drafted"] += 1
            else:
                store.update_prospect(row, last_error=f"follow-up {n} failed validation: {'; '.join(problems)}"[:300])
    store.flush()
    return stats


def close_out(store: Store, cfg: Settings, now: datetime | None = None) -> int:
    now = now or now_utc()
    n = 0
    for row in store.prospects():
        fu2 = parse_iso(row.get("fu2_at", ""))
        if row.get("status") == "followup_2" and fu2 and now - fu2 >= timedelta(days=cfg.done_after_days):
            store.update_prospect(row, status="done")
            n += 1
    store.flush()
    return n
