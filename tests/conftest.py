import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Secrets, Settings  # noqa: E402
from store import LocalStore  # noqa: E402


class FakeResponse:
    def __init__(self, url, text, ctype="text/html"):
        self.url, self.text, self.headers = url, text, {"Content-Type": ctype}


class FakeFetcher:
    """Stands in for web.PoliteFetcher. pages: {url: html}; disallow: set of urls."""

    def __init__(self, pages=None, disallow=(), robots_ok=True):
        self.pages, self.disallow, self.robots_ok = pages or {}, set(disallow), robots_ok
        self.requested = []

    def robots_for(self, url):
        return object() if self.robots_ok else None

    def allowed(self, url):
        return self.robots_ok and url not in self.disallow

    def get(self, url, strict=False, min_delay=None, max_delay=None):
        self.requested.append(url)
        if not self.allowed(url):
            return None
        if url in self.pages:
            return FakeResponse(url, self.pages[url])
        return None


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def secrets():
    return Secrets(brevo_api_key="k", sender_email="ali@hamidbuilds.dev", reply_to_email="ali@hamidbuilds.dev",
                   unsubscribe_base_url="https://script.google.com/macros/s/X/exec", unsubscribe_secret="s3cret",
                   brave_api_key="b", anthropic_api_key="a")


@pytest.fixture
def live_cfg(secrets):
    return Settings({"AUTO_SEND": "TRUE", "DRY_RUN": "FALSE", "REQUIRE_APPROVAL": "FALSE",
                     "BUSINESS_ADDRESS": "1 Test St, Lahore", "SEND_DAYS": "Mon,Tue,Wed,Thu,Fri,Sat,Sun",
                     "SEND_START_HOUR": "0", "SEND_END_HOUR": "24"}, secrets)


@pytest.fixture
def store(tmp_path):
    return LocalStore(str(tmp_path / "state.json"))


@pytest.fixture
def tuesday_10am_sydney():
    # Tue 2026-10-20 10:00 AEDT = Mon 23:00 UTC
    return datetime(2026, 10, 19, 23, 0, tzinfo=timezone.utc)
