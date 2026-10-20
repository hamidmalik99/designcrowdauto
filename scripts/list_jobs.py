"""List currently open DesignCrowd logo/branding jobs to a CSV, for manual outreach.

No API keys, no login. Same rules as the pipeline: robots.txt checked on the
listing and on every job page, one request every 5-10 s, stops on 403/429/captcha.

  python scripts/list_jobs.py                 # writes jobs.csv
  python scripts/list_jobs.py --max-pages 3 --out today.csv

If SHEET_ID and GOOGLE_SERVICE_ACCOUNT_JSON are set, new jobs (by URL) are also
appended to the sheet's "Jobs" tab.

Reminder for the emails you write: never mention DesignCrowd, the contest or the
brief, and only use a business email published on the business's own website.
"""
import argparse
import csv
import os
import re
import sys
from urllib.parse import urljoin

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bs4 import BeautifulSoup  # noqa: E402

from config import CONFIG_DEFAULTS  # noqa: E402
from discover import ANON_NAMES, parse_brief_fields  # noqa: E402
from store import JOBS_HEADERS, iso  # noqa: E402
from web import Blocked, PoliteFetcher  # noqa: E402

COLUMNS = ["business_name", "industry", "location", "website", "title", "url"]


def collect(fetcher, listing_tpl, brief_re, max_pages, log=print):
    if fetcher.robots_for("https://www.designcrowd.com/") is None:
        log("robots.txt could not be read - stopping (treated as disallowed).")
        return []
    if not fetcher.allowed(listing_tpl.format(page=1)):
        log("robots.txt disallows the jobs listing - stopping.")
        return []
    urls = []
    try:
        for page in range(1, max_pages + 1):
            url = listing_tpl.format(page=page)
            r = fetcher.get(url, strict=True, min_delay=5, max_delay=10)
            if r is None:
                break
            new = []
            for a in BeautifulSoup(r.text, "html.parser").find_all("a", href=True):
                href = urljoin(url, a["href"]).split("#")[0].split("?")[0]
                if brief_re.search(href) and href not in urls and href not in new:
                    new.append(href)
            log(f"listing page {page}: {len(new)} new job links")
            if not new:
                break
            urls += new

        rows = []
        for i, u in enumerate(urls, 1):
            if not fetcher.allowed(u):
                log(f"[{i}/{len(urls)}] robots.txt disallows {u} - skipped")
                continue
            r = fetcher.get(u, strict=True, min_delay=5, max_delay=10)
            if r is None:
                continue
            f = parse_brief_fields(r.text)
            name = f.get("business_name", "")
            if name and ANON_NAMES.match(name):
                name = ""
            rows.append({"business_name": name, "industry": f.get("industry", ""),
                         "location": f.get("location", ""), "website": f.get("website", ""),
                         "title": f.get("title", ""), "url": u})
            log(f"[{i}/{len(urls)}] {name or '(no business name)'} - {f.get('title', '')[:70]}")
        return rows
    except Blocked as e:
        log(f"DesignCrowd refused the request ({e}). Stopped; not retrying.")
        return []


def append_to_sheet(rows, sheet_id, sa_json, log=print) -> int:
    """Append jobs whose URL isn't already in the Jobs tab. Returns how many were added."""
    import json

    import gspread

    info = json.loads(sa_json) if sa_json.strip().startswith("{") else None
    gc = gspread.service_account_from_dict(info) if info else gspread.service_account(filename=sa_json)
    sh = gc.open_by_key(sheet_id)
    try:
        ws = sh.worksheet("Jobs")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet("Jobs", rows=1000, cols=len(JOBS_HEADERS))
        ws.update([JOBS_HEADERS], "A1")
    header = ws.row_values(1) or JOBS_HEADERS
    url_col = header.index("url") + 1
    seen = set(ws.col_values(url_col)[1:])
    now = iso()
    new = [[now if h == "found_at" else r.get(h, "") for h in header] for r in rows if r["url"] not in seen]
    if new:
        ws.append_rows(new, value_input_option="RAW")
    log(f"Added {len(new)} new jobs to the sheet's Jobs tab ({len(rows) - len(new)} already there)")
    return len(new)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-pages", type=int, default=5)
    ap.add_argument("--out", default="jobs.csv")
    ap.add_argument("--listing", default=CONFIG_DEFAULTS["DISCOVERY_LISTING_URL"])
    ap.add_argument("--pattern", default=CONFIG_DEFAULTS["DISCOVERY_BRIEF_URL_PATTERN"])
    args = ap.parse_args()

    rows = collect(PoliteFetcher(os.getenv("BOT_INFO_URL", "https://hamidbuilds.dev")),
                   args.listing, re.compile(args.pattern), min(args.max_pages, 50))
    rows.sort(key=lambda r: (r["business_name"] == "", r["business_name"].lower()))
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    named = sum(1 for r in rows if r["business_name"])
    print(f"\nWrote {len(rows)} jobs ({named} name a business) to {args.out}")
    sheet_id, sa = os.getenv("SHEET_ID", ""), os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if rows and sheet_id and sa:
        append_to_sheet(rows, sheet_id, sa)


if __name__ == "__main__":
    main()
