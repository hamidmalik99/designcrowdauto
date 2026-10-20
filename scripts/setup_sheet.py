"""Create/repair the sheet's tabs, headers, Config defaults and the status dropdown.

  python scripts/setup_sheet.py            # uses SHEET_ID + GOOGLE_SERVICE_ACCOUNT_JSON from env/.env

Safe to re-run: existing data and Config values are never overwritten.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gspread  # noqa: E402

from config import CONFIG_DEFAULTS, Secrets  # noqa: E402
from store import CONFIG_HEADERS, JOBS_HEADERS, LOG_HEADERS, PROSPECT_HEADERS, SUPPRESSION_HEADERS  # noqa: E402

STATUSES = ["new", "researched", "skipped", "queued", "approved", "sent", "followup_1", "followup_2",
            "replied", "bounced", "unsubscribed", "done"]
NOTES = {
    "AUTO_SEND": "Kill switch. FALSE = nothing is ever sent.",
    "DRY_RUN": "TRUE = drafts only. Keep TRUE for the first 3-5 days.",
    "REQUIRE_APPROVAL": "TRUE = only rows you set to 'approved' get sent.",
    "DAILY_CAP": "New emails per 24h. Code never allows more than 40.",
    "BUSINESS_ADDRESS": "Required by law in every email footer. Sending is blocked while empty.",
    "PAUSED_REASON": "Set automatically on high bounces/complaints. Clear it to resume.",
    "DISCOVERY_MODE": "auto = crawl public listing if robots.txt allows; manual = only rows you add.",
}


def main():
    s = Secrets()
    info = json.loads(s.google_service_account_json) if s.google_service_account_json.strip().startswith("{") else None
    gc = gspread.service_account_from_dict(info) if info else gspread.service_account(filename=s.google_service_account_json)
    sh = gc.open_by_key(s.sheet_id)
    existing = {w.title: w for w in sh.worksheets()}
    for title, headers in [("Prospects", PROSPECT_HEADERS), ("Suppression", SUPPRESSION_HEADERS),
                           ("Config", CONFIG_HEADERS), ("Log", LOG_HEADERS), ("Jobs", JOBS_HEADERS)]:
        ws = existing.get(title) or sh.add_worksheet(title, rows=1000, cols=len(headers) + 2)
        current = ws.row_values(1)
        if not current:
            ws.update([headers], "A1")
        else:
            missing = [h for h in headers if h not in current]
            if missing:
                ws.update([current + missing], "A1")
        ws.freeze(rows=1)
        existing[title] = ws

    cfg_ws = existing["Config"]
    have = {r[0].strip().upper() for r in cfg_ws.get_all_values()[1:] if r}
    rows = [[k, v, NOTES.get(k, "")] for k, v in CONFIG_DEFAULTS.items() if k not in have]
    if rows:
        cfg_ws.append_rows(rows, value_input_option="RAW")

    p = existing["Prospects"]
    status_col = p.row_values(1).index("status")
    sh.batch_update({"requests": [{"setDataValidation": {
        "range": {"sheetId": p.id, "startRowIndex": 1, "startColumnIndex": status_col, "endColumnIndex": status_col + 1},
        "rule": {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": v} for v in STATUSES]},
                 "strict": True, "showCustomUi": True}}}]})
    if "Sheet1" in existing and len(sh.worksheets()) > 5:
        try:
            sh.del_worksheet(existing["Sheet1"])
        except Exception:
            pass
    print(f"Sheet ready: https://docs.google.com/spreadsheets/d/{s.sheet_id}")


if __name__ == "__main__":
    main()
