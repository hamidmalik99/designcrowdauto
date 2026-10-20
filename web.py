"""Polite HTTP: one identified User-Agent, robots.txt respected on every host,
per-host rate limiting, and a hard stop (no workaround) on 403/429/captcha."""
from __future__ import annotations

import logging
import random
import time
import urllib.robotparser
from urllib.parse import urljoin, urlparse

import requests

log = logging.getLogger(__name__)

CAPTCHA_MARKERS = ("captcha", "cf-challenge", "are you a robot", "verify you are human",
                   "challenge-platform", "access denied")


class Blocked(Exception):
    """The site refused us (403/429/captcha). Callers must stop, not retry around it."""


class PoliteFetcher:
    def __init__(self, bot_info_url: str = "https://hamidbuilds.dev", min_delay: float = 1.0,
                 max_delay: float = 2.0, timeout: int = 20):
        self.user_agent = f"HamidBuildsLeadResearch/1.0 (+{bot_info_url})"
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.user_agent,
                                     "Accept-Language": "en-AU,en;q=0.9"})
        self.min_delay, self.max_delay, self.timeout = min_delay, max_delay, timeout
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._last_hit: dict[str, float] = {}
        self._cache: dict[str, requests.Response] = {}
        self.robots_status: dict[str, str] = {}  # host -> HTTP status / error, for diagnostics
        self.last_status: str = ""

    # --- robots --------------------------------------------------------
    def robots_for(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        """Parsed robots.txt for the URL's host. None means 'could not determine'."""
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.session.get(urljoin(base, "/robots.txt"), timeout=self.timeout)
                self.robots_status[base] = str(r.status_code)
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                elif r.status_code in (404, 410):
                    rp.parse([])  # no robots.txt = everything allowed
                else:
                    rp = None  # 401/403/5xx: unknown -> treat as disallowed
            except requests.RequestException as e:
                log.info("robots.txt fetch failed for %s: %s", base, e)
                self.robots_status[base] = type(e).__name__
                rp = None
            self._robots[base] = rp
        return self._robots[base]

    def allowed(self, url: str) -> bool:
        rp = self.robots_for(url)
        return bool(rp and rp.can_fetch(self.user_agent, url))

    # --- fetching ------------------------------------------------------
    def _throttle(self, host: str, min_delay: float, max_delay: float):
        wait = random.uniform(min_delay, max_delay) - (time.monotonic() - self._last_hit.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    def get(self, url: str, strict: bool = False, min_delay: float | None = None,
            max_delay: float | None = None) -> requests.Response | None:
        """GET a URL if robots.txt allows it.

        strict=True (used for DesignCrowd): raise Blocked on 403/429/captcha so the
        caller stops the whole crawl. strict=False (business sites): return None.
        """
        if url in self._cache:
            return self._cache[url]
        if not self.allowed(url):
            log.info("robots.txt disallows %s", url)
            return None
        host = urlparse(url).netloc
        self._throttle(host, min_delay if min_delay is not None else self.min_delay,
                       max_delay if max_delay is not None else self.max_delay)
        try:
            r = self.session.get(url, timeout=self.timeout, allow_redirects=True)
        except requests.RequestException as e:
            log.info("fetch failed %s: %s", url, e)
            self.last_status = type(e).__name__
            return None
        self.last_status = str(r.status_code)
        body_head = r.text[:5000].lower() if "text" in r.headers.get("Content-Type", "") else ""
        if r.status_code in (403, 429) or (strict and any(m in body_head for m in CAPTCHA_MARKERS)):
            if strict:
                raise Blocked(f"{r.status_code} at {url}")
            return None
        if r.status_code >= 400:
            return None
        self._cache[url] = r
        return r
