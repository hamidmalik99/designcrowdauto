"""Step 3a: find new logo contests on DesignCrowd's PUBLIC listing pages.

Rules (see README "DesignCrowd terms"):
- never logs in, never uses an account, sends no cookies beyond the default session
- robots.txt is checked every run; if the listing or a brief is disallowed (or
  robots.txt can't be read) the run falls back to Manual Intake Mode
- 1 request every 5-10 s, max DISCOVERY_MAX_PAGES (<=50) listing pages per run
- any 403/429/captcha stops the crawl for the day; nothing tries to get around it
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from config import Settings
from store import Store, iso, make_id, normalize_domain
from web import Blocked, PoliteFetcher

log = logging.getLogger(__name__)

DC_ROBOTS_PROBE = "https://www.designcrowd.com/"
ANON_NAMES = re.compile(r"^(tbd|tba|n/?a|none|unknown|anonymous|confidential|to be (decided|determined)|"
                        r"name (tbd|tba|to be decided)|new (business|company|brand)|my (business|company)|"
                        r"not sure( yet)?|undecided|\?+|-+)$", re.I)
FIELD_LABELS = {
    "business_name": ["company name", "business name", "brand name", "organization name",
                      "organisation name", "name to incorporate in the logo"],
    "industry": ["industry", "business type", "type of business", "category"],
    "location": ["location", "country", "based in", "city"],
    "website": ["website", "company website", "web address", "url"],
}


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip(" :-–—\t\n")


def parse_brief(html: str, url: str = "") -> dict:
    """Extract business name / industry / location / website from a public brief page.

    Works from labelled fields (dt/dd, th/td, 'Label: value' lines) and JSON-LD.
    Returns {} if no identifiable business name is present.
    """
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, str] = {}

    def put(field: str, value: str):
        value = _clean(value)
        if value and field not in found and len(value) <= 120:
            found[field] = value

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict):
                org = item.get("hiringOrganization") or item.get("organization") or {}
                if isinstance(org, dict):
                    put("business_name", org.get("name", ""))
                    put("website", org.get("sameAs", "") or org.get("url", ""))
                loc = item.get("jobLocation") or {}
                if isinstance(loc, dict):
                    addr = loc.get("address") or {}
                    if isinstance(addr, dict):
                        put("location", ", ".join(filter(None, [addr.get("addressLocality"),
                                                                addr.get("addressRegion"),
                                                                addr.get("addressCountry")])))

    pairs: list[tuple[str, str]] = []
    for dt in soup.find_all(["dt", "th"]):
        dd = dt.find_next_sibling(["dd", "td"])
        if dd:
            pairs.append((dt.get_text(" "), dd.get_text(" ")))
    for el in soup.find_all(["label", "strong", "b", "h3", "h4", "span"]):
        nxt = el.find_next_sibling()
        if nxt is not None and len(el.get_text(strip=True)) < 40:
            pairs.append((el.get_text(" "), nxt.get_text(" ")))
    for line in soup.get_text("\n").splitlines():
        if ":" in line and len(line) < 200:
            k, v = line.split(":", 1)
            pairs.append((k, v))

    for label, value in pairs:
        lab = _clean(label).lower().rstrip("?")
        for field, names in FIELD_LABELS.items():
            if lab in names:
                put(field, value)

    if "website" in found:
        m = re.search(r"(https?://)?([a-z0-9-]+\.)+[a-z]{2,}(/\S*)?", found["website"], re.I)
        found["website"] = m.group(0) if m else ""
        if found["website"] and "designcrowd" in found["website"].lower():
            found["website"] = ""

    name = found.get("business_name", "")
    if not name or ANON_NAMES.match(name) or len(name) < 2:
        return {}
    found["source_url"] = url
    return found


def discover(store: Store, cfg: Settings, fetcher: PoliteFetcher | None = None) -> dict:
    """Returns a stats dict. Adds new rows with status 'new'."""
    stats = {"found": 0, "notes": ""}
    if cfg.get("DISCOVERY_MODE").lower() != "auto":
        stats["notes"] = "manual intake mode (DISCOVERY_MODE=manual)"
        return stats

    fetcher = fetcher or PoliteFetcher(cfg.secrets.bot_info_url)
    listing_tpl = cfg.get("DISCOVERY_LISTING_URL")
    brief_re = re.compile(cfg.get("DISCOVERY_BRIEF_URL_PATTERN"))

    if fetcher.robots_for(DC_ROBOTS_PROBE) is None:
        stats["notes"] = "robots.txt unreadable -> manual intake mode this run"
        return stats
    if not fetcher.allowed(listing_tpl.format(page=1)):
        stats["notes"] = "robots.txt disallows the listing page -> manual intake mode"
        return stats

    known_ids = {r["id"] for r in store.prospects()}
    known_sources = {r["source_url"] for r in store.prospects() if r.get("source_url")}
    suppressed = store.suppressed_values()
    brief_urls: list[str] = []
    try:
        for page in range(1, cfg.discovery_max_pages + 1):
            url = listing_tpl.format(page=page)
            r = fetcher.get(url, strict=True, min_delay=5, max_delay=10)
            if r is None:
                break
            soup = BeautifulSoup(r.text, "html.parser")
            new_links = []
            for a in soup.find_all("a", href=True):
                href = urljoin(url, a["href"]).split("#")[0]
                if brief_re.search(href) and href not in brief_urls and href not in known_sources:
                    new_links.append(href)
            if not new_links:
                break
            brief_urls.extend(new_links)
            if "{page}" not in listing_tpl:
                break

        disallowed = 0
        for burl in brief_urls:
            if not fetcher.allowed(burl):
                disallowed += 1
                continue
            r = fetcher.get(burl, strict=True, min_delay=5, max_delay=10)
            if r is None:
                continue
            info = parse_brief(r.text, burl)
            if not info:
                continue  # no identifiable business named -> skip, never guess
            domain = normalize_domain(info.get("website", ""))
            pid = make_id(domain, info["business_name"], info.get("location", ""))
            if pid in known_ids or (domain and domain in suppressed):
                continue
            store.add_prospect({
                "id": pid, "source_url": burl, "date_found": iso(),
                "business_name": info["business_name"], "industry": info.get("industry", ""),
                "location": info.get("location", ""), "website": info.get("website", ""),
                "domain": domain, "status": "new",
            })
            known_ids.add(pid)
            stats["found"] += 1
        if disallowed:
            stats["notes"] += f"{disallowed} brief pages disallowed by robots.txt; "
        if not brief_urls:
            stats["notes"] += "no brief links found - verify DISCOVERY_LISTING_URL / DISCOVERY_BRIEF_URL_PATTERN; "
    except Blocked as e:
        stats["notes"] += f"stopped: DesignCrowd returned {e} (not retried); "
        stats["errors"] = 1
    return stats
