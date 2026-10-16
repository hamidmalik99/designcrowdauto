"""Orchestrator.

  python main.py daily [--dry-run] [--limit N]   # inbox -> discover -> research -> write -> follow-up drafts -> summary
  python main.py send  [--dry-run]               # inbox -> send due emails (hourly during sending windows)
  python main.py inbox                           # replies / bounces / unsubscribes only
  python main.py summary                         # email the daily summary now

Add --local state.json to run against a local JSON file instead of the Google Sheet.
"""
from __future__ import annotations

import argparse
import logging
import sys

import discover
import followups
import inbox
import research
import send
import summary
import write
from config import Secrets, Settings
from store import LocalStore, SheetStore, Store, iso


def open_store(args, secrets: Secrets) -> Store:
    if args.local:
        return LocalStore(args.local)
    if not (secrets.sheet_id and secrets.google_service_account_json):
        sys.exit("SHEET_ID and GOOGLE_SERVICE_ACCOUNT_JSON must be set (or use --local state.json)")
    return SheetStore(secrets.sheet_id, secrets.google_service_account_json)


def write_log(store: Store, step: str, **counts):
    entry = {"run_at": iso(), "step": step, **counts}
    store.log(entry)
    logging.info("run log: %s", {k: v for k, v in entry.items() if v not in ("", 0, None)})


def step_daily(store: Store, cfg: Settings, args) -> None:
    alert = lambda subj, body: summary.send_plain(cfg, subj, body)  # noqa: E731
    notes, errors = [], 0

    ib = inbox.run_inbox(store, cfg, alert)
    if ib.get("note"):
        notes.append(ib["note"])
    if ib.get("paused"):
        notes.append(f"auto-paused: {ib['paused']}")

    d = discover.discover(store, cfg)
    notes.append(f"discover: {d.get('notes') or 'ok'}")
    errors += d.get("errors", 0)
    store.flush()

    r = research.run_research(store, cfg, limit=args.limit)
    w = write.run_write(store, cfg)
    f = followups.prepare(store, cfg)
    done = followups.close_out(store, cfg)
    errors += r["errors"] + w["errors"] + f["errors"]
    if done:
        notes.append(f"{done} sequences marked done")
    if f["drafted"]:
        notes.append(f"{f['drafted']} follow-ups drafted")

    write_log(store, "daily", found=d.get("found", 0), researched=r["researched"],
              skipped=r["skipped"] + w["skipped"], queued=w["queued"], replies=ib.get("replies", 0),
              bounces=ib.get("bounces", 0), unsubscribes=ib.get("unsubscribes", 0), errors=errors,
              notes="; ".join(notes))
    store.flush()
    if not args.no_summary:
        summary.run_summary(store, cfg)


def step_send(store: Store, cfg: Settings, args, secrets: Secrets) -> None:
    alert = lambda subj, body: summary.send_plain(cfg, subj, body)  # noqa: E731
    ib = inbox.run_inbox(store, cfg, alert)

    def cfg_loader() -> Settings:
        return Settings(store.reload_config(), secrets, force_dry_run=args.dry_run)

    s = send.run_send(store, cfg_loader, max_runtime_s=args.max_runtime,
                      reply_checker=inbox.has_reply(cfg))
    write_log(store, "send", sent=s["sent"], followups_sent=s["followups_sent"], replies=ib.get("replies", 0),
              bounces=ib.get("bounces", 0), unsubscribes=ib.get("unsubscribes", 0), errors=s["errors"],
              notes=(s["notes"] + (f" auto-paused: {ib['paused']}" if ib.get("paused") else "")).strip())
    store.flush()


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="Automated outreach to new logo-contest businesses")
    p.add_argument("step", choices=["daily", "send", "inbox", "summary"])
    p.add_argument("--dry-run", action="store_true", help="never send; write drafts to the sheet")
    p.add_argument("--limit", type=int, default=None, help="max prospects to research this run")
    p.add_argument("--local", help="use a local JSON state file instead of Google Sheets")
    p.add_argument("--no-summary", action="store_true")
    p.add_argument("--max-runtime", type=int, default=3300, help="send step: stop after N seconds")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    secrets = Secrets()
    store = open_store(args, secrets)
    cfg = Settings(store.config(), secrets, force_dry_run=args.dry_run)
    logging.info("mode: dry_run=%s auto_send=%s require_approval=%s daily_cap=%s",
                 cfg.dry_run, cfg.auto_send, cfg.require_approval, cfg.daily_cap)

    if args.step == "daily":
        step_daily(store, cfg, args)
    elif args.step == "send":
        step_send(store, cfg, args, secrets)
    elif args.step == "inbox":
        ib = inbox.run_inbox(store, cfg, lambda s_, b: summary.send_plain(cfg, s_, b))
        write_log(store, "inbox", replies=ib.get("replies", 0), bounces=ib.get("bounces", 0),
                  unsubscribes=ib.get("unsubscribes", 0), notes=ib.get("note", ""))
    elif args.step == "summary":
        summary.run_summary(store, cfg)
    store.flush()


if __name__ == "__main__":
    main()
