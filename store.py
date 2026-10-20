"""Storage: Google Sheet (production) or a local JSON file (tests / offline dry runs).

Both expose the same small interface so every pipeline step is backend-agnostic.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

PROSPECT_HEADERS = [
    "id", "source_url", "date_found", "business_name", "industry", "location", "timezone",
    "website", "domain", "has_website", "audit_notes", "contact_email", "contact_name",
    "email_source_url", "quality_score", "search_confidence", "status", "skip_reason",
    "email_1_subject", "email_1_body", "fu1_body", "fu2_body",
    "sent_at", "fu1_at", "fu2_at", "replied_at", "reply_thread_url",
    "thread_id", "message_id", "last_error",
]
SUPPRESSION_HEADERS = ["value", "type", "reason", "source", "added_at"]
CONFIG_HEADERS = ["key", "value", "notes"]
JOBS_HEADERS = ["found_at", "business_name", "industry", "location", "website", "title", "url"]
LOG_HEADERS = ["run_at", "step", "found", "researched", "skipped", "queued", "sent",
               "followups_sent", "replies", "bounces", "unsubscribes", "errors", "notes"]

ACTIVE_STATUSES = {"sent", "followup_1", "followup_2"}
TERMINAL_STATUSES = {"skipped", "replied", "bounced", "unsubscribed", "done"}


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime | None = None) -> str:
    return (dt or now_utc()).isoformat()


def parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def normalize_domain(url_or_domain: str) -> str:
    s = (url_or_domain or "").strip().lower()
    if not s:
        return ""
    if "://" not in s:
        s = "http://" + s
    host = urlparse(s).hostname or ""
    return re.sub(r"^www\d?\.", "", host)


def make_id(domain: str = "", business_name: str = "", location: str = "") -> str:
    key = normalize_domain(domain) if domain else f"name:{business_name.strip().lower()}|{location.strip().lower()}"
    return hashlib.sha1(key.encode()).hexdigest()[:12]


SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
                 "https://www.googleapis.com/auth/drive"]


def google_creds_available(service_account_json: str = "") -> bool:
    return bool(service_account_json.strip() or os.getenv("GOOGLE_APPLICATION_CREDENTIALS"))


def gspread_client(service_account_json: str = ""):
    """Service-account JSON (inline or a path) if given; otherwise Application Default
    Credentials, e.g. the keyless Workload Identity Federation login set up by the
    google-github-actions/auth step in GitHub Actions."""
    import gspread

    sa = service_account_json.strip()
    if sa:
        return gspread.service_account_from_dict(json.loads(sa)) if sa.startswith("{") \
            else gspread.service_account(filename=sa)
    import google.auth

    creds, _ = google.auth.default(scopes=SHEETS_SCOPES)
    return gspread.authorize(creds)


class Store:
    """Interface. Rows are dicts with a private '_row' key (1-based sheet row)."""

    def prospects(self) -> list[dict]: ...
    def add_prospect(self, data: dict) -> dict: ...
    def update_prospect(self, row: dict, **fields) -> None: ...
    def suppression(self) -> list[dict]: ...
    def add_suppression(self, value: str, type_: str, reason: str, source: str) -> None: ...
    def config(self) -> dict[str, str]: ...
    def reload_config(self) -> dict[str, str]:
        return self.config()
    def set_config(self, key: str, value: str) -> None: ...
    def log(self, entry: dict) -> None: ...
    def logs(self) -> list[dict]: ...
    def flush(self) -> None: ...

    # --- shared helpers -------------------------------------------------
    def suppressed_values(self) -> set[str]:
        return {r["value"].strip().lower() for r in self.suppression() if r.get("value")}

    def is_suppressed(self, email: str = "", domain: str = "") -> bool:
        vals = self.suppressed_values()
        email = (email or "").strip().lower()
        domain = normalize_domain(domain) if domain else (email.split("@")[-1] if "@" in email else "")
        return bool((email and email in vals) or (domain and domain in vals))

    def suppress(self, value: str, type_: str, reason: str, source: str) -> None:
        value = (value or "").strip().lower()
        if value and value not in self.suppressed_values():
            self.add_suppression(value, type_, reason, source)


class LocalStore(Store):
    def __init__(self, path: str):
        self.path = path
        if os.path.exists(path):
            with open(path) as f:
                self.data = json.load(f)
        else:
            self.data = {"Prospects": [], "Suppression": [], "Config": {}, "Log": []}
        for i, r in enumerate(self.data["Prospects"]):
            r["_row"] = i + 2

    def prospects(self):
        return self.data["Prospects"]

    def add_prospect(self, data):
        row = {h: "" for h in PROSPECT_HEADERS}
        row.update({k: v for k, v in data.items() if k in PROSPECT_HEADERS})
        row["_row"] = len(self.data["Prospects"]) + 2
        self.data["Prospects"].append(row)
        return row

    def update_prospect(self, row, **fields):
        for k, v in fields.items():
            if k not in PROSPECT_HEADERS:
                raise KeyError(k)
            row[k] = "" if v is None else str(v)

    def suppression(self):
        return self.data["Suppression"]

    def add_suppression(self, value, type_, reason, source):
        self.data["Suppression"].append({"value": value, "type": type_, "reason": reason,
                                         "source": source, "added_at": iso()})

    def config(self):
        return dict(self.data["Config"])

    def set_config(self, key, value):
        self.data["Config"][key] = value

    def log(self, entry):
        self.data["Log"].append({h: str(entry.get(h, "")) for h in LOG_HEADERS})

    def logs(self):
        return self.data["Log"]

    def flush(self):
        out = dict(self.data)
        out["Prospects"] = [{k: v for k, v in r.items() if k != "_row"} for r in self.data["Prospects"]]
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(out, f, indent=2)
        os.replace(tmp, self.path)


class SheetStore(Store):
    """Google Sheets backend via a service account (gspread).

    Prospect cell edits are buffered and written in one batch on flush() to stay
    well inside the Sheets API quota.
    """

    def __init__(self, sheet_id: str, service_account_json: str = ""):
        self.sh = gspread_client(service_account_json).open_by_key(sheet_id)
        self.ws = {t: self.sh.worksheet(t) for t in ("Prospects", "Suppression", "Config", "Log")}
        self._pending: list[dict] = []
        self._load()

    def _load(self):
        values = self.ws["Prospects"].get_all_values()
        self.headers = values[0] if values else PROSPECT_HEADERS
        missing = [h for h in PROSPECT_HEADERS if h not in self.headers]
        if missing:
            raise RuntimeError(f"Prospects tab is missing columns {missing}; run scripts/setup_sheet.py")
        self._rows = []
        for i, vals in enumerate(values[1:], start=2):
            r = {h: (vals[j] if j < len(vals) else "") for j, h in enumerate(self.headers)}
            r["_row"] = i
            self._rows.append(r)
        self._supp = self.ws["Suppression"].get_all_records(expected_headers=SUPPRESSION_HEADERS)
        self.reload_config()

    def prospects(self):
        return self._rows

    def add_prospect(self, data):
        row = {h: "" for h in self.headers}
        row.update({k: str(v) for k, v in data.items() if k in self.headers})
        self.ws["Prospects"].append_row([row[h] for h in self.headers], value_input_option="RAW")
        row["_row"] = (self._rows[-1]["_row"] + 1) if self._rows else 2
        self._rows.append(row)
        return row

    def update_prospect(self, row, **fields):
        import gspread.utils as gu

        for k, v in fields.items():
            if k not in self.headers:
                raise KeyError(k)
            v = "" if v is None else str(v)
            row[k] = v
            col = self.headers.index(k) + 1
            self._pending.append({"range": gu.rowcol_to_a1(row["_row"], col), "values": [[v]]})
        if len(self._pending) > 200:
            self.flush()

    def suppression(self):
        return self._supp

    def add_suppression(self, value, type_, reason, source):
        rec = {"value": value, "type": type_, "reason": reason, "source": source, "added_at": iso()}
        self.ws["Suppression"].append_row([rec[h] for h in SUPPRESSION_HEADERS], value_input_option="RAW")
        self._supp.append(rec)

    def config(self):
        return dict(self._cfg)

    def reload_config(self):
        recs = self.ws["Config"].get_all_records()
        self._cfg = {str(r.get("key", "")).strip().upper(): str(r.get("value", "")) for r in recs}
        self._cfg_rows = {str(r.get("key", "")).strip().upper(): i + 2 for i, r in enumerate(recs)}
        return dict(self._cfg)

    def set_config(self, key, value):
        key = key.upper()
        if key in self._cfg_rows:
            self.ws["Config"].update_cell(self._cfg_rows[key], 2, value)
        else:
            self.ws["Config"].append_row([key, value, ""], value_input_option="RAW")
        self._cfg[key] = value

    def log(self, entry):
        self.ws["Log"].append_row([str(entry.get(h, "")) for h in LOG_HEADERS], value_input_option="RAW")

    def logs(self):
        return self.ws["Log"].get_all_records(expected_headers=LOG_HEADERS)

    def flush(self):
        if self._pending:
            self.ws["Prospects"].batch_update(self._pending, value_input_option="RAW")
            self._pending = []
