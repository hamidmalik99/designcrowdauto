"""Settings: secrets come from the environment (.env / GitHub secrets),
business settings come from the sheet's Config tab so Ali can change them
without touching code."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

# Hard limits enforced in code no matter what the Config tab says.
HARD_DAILY_CAP = 40
HARD_FOLLOWUP_CAP = 40
DOMAIN_COOLDOWN_DAYS = 180
MAX_FOLLOWUPS = 2

# Defaults written to the Config tab by scripts/setup_sheet.py.
CONFIG_DEFAULTS: dict[str, str] = {
    "AUTO_SEND": "FALSE",            # global kill switch: FALSE = nothing is ever sent
    "DRY_RUN": "TRUE",               # TRUE = drafts only; keep TRUE for the first 3-5 days
    "REQUIRE_APPROVAL": "TRUE",      # TRUE = only rows Ali sets to status 'approved' are sent
    "DAILY_CAP": "20",
    "FOLLOWUP_DAILY_CAP": "20",
    "FOLLOWUP_1_DAYS": "4",
    "FOLLOWUP_2_DAYS": "9",
    "DONE_AFTER_DAYS": "7",          # days after follow-up 2 before a row is marked done
    "MIN_GAP_MINUTES": "4",
    "MAX_GAP_MINUTES": "9",
    "SEND_START_HOUR": "9",          # recipient local time
    "SEND_END_HOUR": "16",
    "SEND_DAYS": "Tue,Wed,Thu",
    "TARGET_COUNTRIES": "AU,UK,US,NZ,CA",
    "MIN_QUALITY_SCORE": "5",
    "RESEARCH_PER_RUN": "25",
    "DISCOVERY_MODE": "auto",        # auto | manual
    # Public "open logo & branding jobs" listing and brief URL shapes, taken from search-engine results
    # (Oct 2026). The ?page= parameter is unconfirmed; page 1 works either way.
    "DISCOVERY_LISTING_URL": "https://www.designcrowd.com/jobs/open/corporateidentity/?page={page}",
    "DISCOVERY_BRIEF_URL_PATTERN": r"^https://www\.designcrowd\.com/(jobs/job|logo-design/contest)/\d+",
    "DISCOVERY_MAX_PAGES": "50",
    "SENDER_NAME": "Hamid Ali Asad",
    "SENDER_FIRST_NAME": "Ali",
    "SENDER_TITLE": "WordPress Developer & SEO Specialist",
    "PORTFOLIO_URL": "https://hamidbuilds.dev",
    "BUSINESS_ADDRESS": "",          # REQUIRED before sending (CAN-SPAM / Spam Act)
    "PROOF_EXAMPLE": "",             # one REAL past project; empty = emails cite no example
    "SUMMARY_TO": "",
    "SUMMARY_HOUR_UTC": "21",        # 21:00 UTC = 7am AEST / 8am AEDT
    "PAUSED_REASON": "",
}


def _truthy(v: str | None) -> bool:
    return str(v or "").strip().upper() in {"TRUE", "YES", "1", "Y", "ON"}


def _int(v: str | None, default: int) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


@dataclass
class Secrets:
    google_service_account_json: str = field(default_factory=lambda: os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", ""))
    sheet_id: str = field(default_factory=lambda: os.getenv("SHEET_ID", ""))
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    claude_model: str = field(default_factory=lambda: os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5"))
    brave_api_key: str = field(default_factory=lambda: os.getenv("BRAVE_API_KEY", ""))
    serper_api_key: str = field(default_factory=lambda: os.getenv("SERPER_API_KEY", ""))
    pagespeed_api_key: str = field(default_factory=lambda: os.getenv("PAGESPEED_API_KEY", ""))
    hunter_api_key: str = field(default_factory=lambda: os.getenv("HUNTER_API_KEY", ""))
    brevo_api_key: str = field(default_factory=lambda: os.getenv("BREVO_API_KEY", ""))
    sender_email: str = field(default_factory=lambda: os.getenv("SENDER_EMAIL", ""))
    reply_to_email: str = field(default_factory=lambda: os.getenv("REPLY_TO_EMAIL", ""))
    unsubscribe_base_url: str = field(default_factory=lambda: os.getenv("UNSUBSCRIBE_BASE_URL", ""))
    unsubscribe_secret: str = field(default_factory=lambda: os.getenv("UNSUBSCRIBE_SECRET", ""))
    unsubscribe_mailto: str = field(default_factory=lambda: os.getenv("UNSUBSCRIBE_MAILTO", ""))
    gmail_client_id: str = field(default_factory=lambda: os.getenv("GMAIL_CLIENT_ID", ""))
    gmail_client_secret: str = field(default_factory=lambda: os.getenv("GMAIL_CLIENT_SECRET", ""))
    gmail_refresh_token: str = field(default_factory=lambda: os.getenv("GMAIL_REFRESH_TOKEN", ""))
    bot_info_url: str = field(default_factory=lambda: os.getenv("BOT_INFO_URL", "https://hamidbuilds.dev"))


class Settings:
    """Merged view of Config tab values (with defaults) + secrets."""

    def __init__(self, sheet_config: dict[str, str] | None = None, secrets: Secrets | None = None,
                 force_dry_run: bool = False):
        self.raw = dict(CONFIG_DEFAULTS)
        for k, v in (sheet_config or {}).items():
            if str(v).strip() != "":
                self.raw[k.strip().upper()] = str(v).strip()
        self.secrets = secrets or Secrets()
        self._force_dry_run = force_dry_run

    def get(self, key: str, default: str = "") -> str:
        return self.raw.get(key, default)

    @property
    def auto_send(self) -> bool:
        return _truthy(self.raw["AUTO_SEND"]) and not self.raw.get("PAUSED_REASON")

    @property
    def dry_run(self) -> bool:
        return self._force_dry_run or _truthy(self.raw["DRY_RUN"])

    @property
    def require_approval(self) -> bool:
        return _truthy(self.raw["REQUIRE_APPROVAL"])

    @property
    def daily_cap(self) -> int:
        return max(0, min(_int(self.raw["DAILY_CAP"], 20), HARD_DAILY_CAP))

    @property
    def followup_cap(self) -> int:
        return max(0, min(_int(self.raw["FOLLOWUP_DAILY_CAP"], 20), HARD_FOLLOWUP_CAP))

    @property
    def followup_1_days(self) -> int:
        return _int(self.raw["FOLLOWUP_1_DAYS"], 4)

    @property
    def followup_2_days(self) -> int:
        return _int(self.raw["FOLLOWUP_2_DAYS"], 9)

    @property
    def done_after_days(self) -> int:
        return _int(self.raw["DONE_AFTER_DAYS"], 7)

    @property
    def gap_minutes(self) -> tuple[int, int]:
        lo = max(1, _int(self.raw["MIN_GAP_MINUTES"], 4))
        hi = max(lo, _int(self.raw["MAX_GAP_MINUTES"], 9))
        return lo, hi

    @property
    def send_hours(self) -> tuple[int, int]:
        return _int(self.raw["SEND_START_HOUR"], 9), _int(self.raw["SEND_END_HOUR"], 16)

    @property
    def send_days(self) -> set[int]:
        names = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
        days = {names.index(d.strip().upper()[:3]) for d in self.raw["SEND_DAYS"].split(",")
                if d.strip().upper()[:3] in names}
        return days or {1, 2, 3}

    @property
    def target_countries(self) -> set[str]:
        return {c.strip().upper() for c in self.raw["TARGET_COUNTRIES"].split(",") if c.strip()}

    @property
    def min_quality_score(self) -> int:
        return _int(self.raw["MIN_QUALITY_SCORE"], 5)

    @property
    def research_per_run(self) -> int:
        return _int(self.raw["RESEARCH_PER_RUN"], 25)

    @property
    def discovery_max_pages(self) -> int:
        return min(_int(self.raw["DISCOVERY_MAX_PAGES"], 50), 50)

    @property
    def summary_hour_utc(self) -> int:
        return _int(self.raw["SUMMARY_HOUR_UTC"], 21)

    def sending_blockers(self) -> list[str]:
        """Reasons real sending must not happen. Empty list = OK to send."""
        out = []
        if not self.auto_send:
            out.append("AUTO_SEND is FALSE" + (f" (paused: {self.raw['PAUSED_REASON']})" if self.raw.get("PAUSED_REASON") else ""))
        if self.dry_run:
            out.append("dry-run mode")
        if not self.raw.get("BUSINESS_ADDRESS"):
            out.append("BUSINESS_ADDRESS is empty in Config")
        s = self.secrets
        for name, val in [("BREVO_API_KEY", s.brevo_api_key), ("SENDER_EMAIL", s.sender_email),
                          ("UNSUBSCRIBE_BASE_URL", s.unsubscribe_base_url),
                          ("UNSUBSCRIBE_SECRET", s.unsubscribe_secret)]:
            if not val:
                out.append(f"{name} not set")
        return out
