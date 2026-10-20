import discover
import geo
import research
from config import Settings
from conftest import FakeFetcher
from store import make_id


# --- geo ---------------------------------------------------------------------
def test_geo_resolution():
    assert geo.resolve("Brisbane, QLD") == ("AU", "Australia/Brisbane")
    assert geo.resolve("Perth, WA, Australia") == ("AU", "Australia/Perth")
    assert geo.resolve("Seattle, WA") == ("US", "America/Los_Angeles")  # WA is not Western Australia here
    assert geo.resolve("Austin, Texas") == ("US", "America/Chicago")
    assert geo.resolve("Manchester") == ("UK", "Europe/London")
    assert geo.resolve("Calgary, Alberta") == ("CA", "America/Edmonton")
    assert geo.resolve("", "smithplumbing.com.au") == ("AU", "Australia/Sydney")
    assert geo.resolve("Wellington") == ("NZ", "Pacific/Auckland")
    assert geo.resolve("", "") == ("", "")


# --- discovery ---------------------------------------------------------------
BRIEF_HTML = """<html><body><h1>Logo for a plumbing company</h1>
<dl><dt>Company name</dt><dd>Harbour Plumbing Co</dd>
<dt>Industry</dt><dd>Plumbing</dd><dt>Location</dt><dd>Sydney, NSW, Australia</dd>
<dt>Website</dt><dd>harbourplumbing.com.au</dd></dl></body></html>"""


def test_parse_brief_labelled_fields():
    info = discover.parse_brief(BRIEF_HTML, "https://www.designcrowd.com/design/123456")
    assert info["business_name"] == "Harbour Plumbing Co"
    assert info["industry"] == "Plumbing"
    assert info["location"].startswith("Sydney")
    assert info["website"] == "harbourplumbing.com.au"


def test_parse_brief_skips_anonymous_names():
    html = BRIEF_HTML.replace("Harbour Plumbing Co", "TBD")
    assert discover.parse_brief(html) == {}
    assert discover.parse_brief("<html><body>Need a logo</body></html>") == {}


def test_discover_falls_back_to_manual_when_robots_disallows(store):
    cfg = Settings({})
    listing = cfg.get("DISCOVERY_LISTING_URL").format(page=1)
    f = FakeFetcher(disallow={listing})
    stats = discover.discover(store, cfg, f)
    assert stats["found"] == 0 and "manual" in stats["notes"]
    assert f.requested == []  # nothing fetched from DesignCrowd


def test_discover_falls_back_when_robots_unreadable(store):
    f = FakeFetcher(robots_ok=False)
    stats = discover.discover(store, Settings({}), f)
    assert "manual" in stats["notes"] and f.requested == []


def test_discover_respects_manual_mode(store):
    f = FakeFetcher()
    stats = discover.discover(store, Settings({"DISCOVERY_MODE": "manual"}), f)
    assert "manual" in stats["notes"] and f.requested == []


def test_discover_adds_identifiable_briefs(store, monkeypatch):
    cfg = Settings({"DISCOVERY_MAX_PAGES": "2"})
    listing1 = cfg.get("DISCOVERY_LISTING_URL").format(page=1)
    brief = "https://www.designcrowd.com/jobs/job/1234567"
    anon = "https://www.designcrowd.com/logo-design/contest/7654321"
    f = FakeFetcher(pages={
        listing1: f'<a href="{brief}">a</a><a href="{anon}">b</a>',
        brief: BRIEF_HTML,
        anon: BRIEF_HTML.replace("Harbour Plumbing Co", "Name TBD"),
    })
    stats = discover.discover(store, cfg, f)
    assert stats["found"] == 1
    row = store.prospects()[0]
    assert row["status"] == "new" and row["domain"] == "harbourplumbing.com.au"
    # second run: deduped
    assert discover.discover(store, cfg, f)["found"] == 0


# --- research helpers --------------------------------------------------------
def test_email_extraction_and_filters():
    html = """<a href="mailto:Info@HarbourPlumbing.com.au?subject=hi">email</a>
    <p>Call Jane Smith on jane.smith@harbourplumbing.com.au</p>
    <p>photo icon@2x.png  someone@othercompany.com  harbourplumbing@gmail.com  bob@gmail.com</p>
    <span class="__cf_email__" data-cfemail="5d35383131321d3c2f3c3f343e733e3230">x</span>"""
    emails = research.extract_emails(html)
    assert "info@harbourplumbing.com.au" in emails
    assert "jane.smith@harbourplumbing.com.au" in emails
    assert not any("2x" in e for e in emails)
    ok = lambda e: research.email_acceptable(e, "harbourplumbing.com.au", "Harbour Plumbing Co")  # noqa: E731
    assert ok("info@harbourplumbing.com.au")
    assert not ok("someone@othercompany.com")      # not their domain
    assert ok("harbourplumbing@gmail.com")         # business mailbox shown on their site
    assert not ok("bob@gmail.com")                 # looks personal -> rejected


def test_cfemail_decoding():
    # 'hello@arabic.co' style obfuscation used by Cloudflare
    key = 0x42
    plain = "hello@example.org"
    enc = f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in plain)
    assert research._decode_cfemail(enc) == plain


def test_contact_name_only_when_on_page():
    assert research.contact_name_for("jane.smith@x.com", "Meet Jane Smith, owner") == "Jane Smith"
    assert research.contact_name_for("jane@x.com", "Meet Jane, owner") == "Jane"
    assert research.contact_name_for("jane@x.com", "no names here") == ""
    assert research.contact_name_for("info@x.com", "Info desk") == ""


def test_no_unsolicited_statement_detected():
    assert research.NO_UNSOLICITED_RE.search("Please do not send unsolicited emails to this address.")
    assert research.NO_UNSOLICITED_RE.search("No marketing emails please")
    assert not research.NO_UNSOLICITED_RE.search("Email us for a free quote")


def test_pick_website_prefers_matching_domain_and_skips_directories():
    results = [
        {"url": "https://www.yelp.com.au/biz/harbour-plumbing", "title": "Harbour Plumbing Co - Yelp", "snippet": ""},
        {"url": "https://www.facebook.com/harbourplumbing", "title": "Harbour Plumbing Co | Facebook", "snippet": ""},
        {"url": "https://harbourplumbing.com.au/services", "title": "Harbour Plumbing Co | Sydney Plumbers",
         "snippet": "Sydney plumbers"},
        {"url": "https://randomblog.com/top-10-plumbers", "title": "Top 10 plumbers", "snippet": ""},
    ]
    url, conf, kind = research.pick_website("Harbour Plumbing Co", "Sydney NSW", results)
    assert url == "https://harbourplumbing.com.au/" and kind == "website" and conf >= 0.8


def test_pick_website_social_only():
    results = [{"url": "https://www.facebook.com/zestcafe", "title": "Zest Cafe | Facebook", "snippet": ""}]
    assert research.pick_website("Zest Cafe", "", results)[2] == "social"


HOME = """<html><head><title>Hi</title></head><body>
<a href="/contact">Contact us</a><p>&copy; 2019 Harbour Plumbing Co. Meet Jane Smith, owner.</p></body></html>"""
CONTACT = '<html><body><a href="mailto:jane.smith@harbourplumbing.com.au">Email Jane</a> Jane Smith</body></html>'


def test_audit_reports_only_checked_facts(monkeypatch, live_cfg):
    monkeypatch.setattr(research, "pagespeed", lambda cfg, url: {"score": 31, "lcp_s": 6.2})
    notes, signals = research.audit_site(live_cfg, FakeFetcher(), "https://harbourplumbing.com.au/", HOME,
                                         "https://harbourplumbing.com.au/", https_ok=False)
    text = " ".join(notes)
    assert "31/100" in text and "6.2s" in text and "HTTPS" in text and "viewport" in text
    assert "meta description" in text and "H1" in text and "2019" in text and "tap-to-call" in text
    assert signals["small_business"] is True


def test_research_manual_intake_end_to_end(store, live_cfg, monkeypatch):
    monkeypatch.setattr(research, "pagespeed", lambda cfg, url: {"score": 40, "lcp_s": 5.0})
    monkeypatch.setattr(research, "web_search", lambda cfg, q: [
        {"url": "https://harbourplumbing.com.au/", "title": "Harbour Plumbing Co | Sydney", "snippet": "Sydney"}])
    row = store.add_prospect({"id": make_id("", "Harbour Plumbing Co", "Sydney"), "business_name": "Harbour Plumbing Co",
                              "location": "Sydney, NSW", "industry": "Plumbing", "status": "new"})
    f = FakeFetcher(pages={"https://harbourplumbing.com.au/": HOME,
                           "https://harbourplumbing.com.au/contact": CONTACT})
    stats = research.run_research(store, live_cfg, fetcher=f)
    assert stats["researched"] == 1, row
    assert row["status"] == "researched"
    assert row["contact_email"] == "jane.smith@harbourplumbing.com.au"
    assert row["contact_name"] == "Jane Smith"
    assert row["email_source_url"] == "https://harbourplumbing.com.au/contact"
    assert row["timezone"] == "Australia/Sydney" and int(row["quality_score"]) == 10
    assert row["id"] == make_id("harbourplumbing.com.au")


def test_research_skips_without_published_email(store, live_cfg, monkeypatch):
    monkeypatch.setattr(research, "pagespeed", lambda cfg, url: {})
    row = store.add_prospect({"business_name": "Harbour Plumbing Co", "website": "harbourplumbing.com.au",
                              "location": "Sydney", "status": "new"})
    f = FakeFetcher(pages={"https://harbourplumbing.com.au/": HOME})
    research.run_research(store, live_cfg, fetcher=f)
    assert row["status"] == "skipped" and row["skip_reason"] == "no published email"


def test_research_skips_when_site_refuses_unsolicited(store, live_cfg, monkeypatch):
    monkeypatch.setattr(research, "pagespeed", lambda cfg, url: {})
    row = store.add_prospect({"business_name": "Harbour Plumbing Co", "website": "harbourplumbing.com.au",
                              "location": "Sydney", "status": "new"})
    contact = CONTACT.replace("</body>", "<p>We do not accept unsolicited sales emails.</p></body>")
    f = FakeFetcher(pages={"https://harbourplumbing.com.au/": HOME, "https://harbourplumbing.com.au/contact": contact})
    research.run_research(store, live_cfg, fetcher=f)
    assert row["status"] == "skipped" and "unsolicited" in row["skip_reason"]


def test_research_skips_suppressed_domain_and_duplicates(store, live_cfg, monkeypatch):
    monkeypatch.setattr(research, "pagespeed", lambda cfg, url: {})
    store.suppress("harbourplumbing.com.au", "domain", "unsubscribed", "test")
    row = store.add_prospect({"business_name": "Harbour Plumbing Co", "website": "harbourplumbing.com.au",
                              "status": "new"})
    research.run_research(store, live_cfg, fetcher=FakeFetcher())
    assert row["status"] == "skipped" and "suppression" in row["skip_reason"]


def test_list_jobs_collects_all_jobs_including_unnamed():
    import re as _re
    import sys as _sys
    _sys.path.insert(0, "scripts")
    import list_jobs
    cfg = Settings({})
    listing = cfg.get("DISCOVERY_LISTING_URL")
    j1, j2 = "https://www.designcrowd.com/jobs/job/4113748", "https://www.designcrowd.com/logo-design/contest/3259402"
    f = FakeFetcher(pages={
        listing.format(page=1): f'<a href="{j1}?ref=x">a</a><a href="/logo-design/contest/3259402">b</a>'
                                '<a href="/jobs/open/corporateidentity/">nav</a>',
        j1: BRIEF_HTML,
        j2: "<html><head><title>Logo brief for a company in United States</title></head><body></body></html>",
    })
    rows = list_jobs.collect(f, listing, _re.compile(cfg.get("DISCOVERY_BRIEF_URL_PATTERN")), 3, log=lambda m: None)
    assert [r["url"] for r in rows] == [j1, j2]
    assert rows[0]["business_name"] == "Harbour Plumbing Co" and rows[1]["business_name"] == ""
    assert "United States" in rows[1]["title"]
    assert list_jobs.collect(FakeFetcher(robots_ok=False), listing, _re.compile("x"), 3, log=lambda m: None) == []


def test_gspread_client_uses_keyless_credentials(monkeypatch):
    import google.auth
    import gspread

    import store
    seen = {}
    monkeypatch.setattr(google.auth, "default", lambda scopes: (seen.setdefault("scopes", scopes), "proj"))
    monkeypatch.setattr(gspread, "authorize", lambda creds: ("client", creds))
    assert store.gspread_client("")[0] == "client"
    assert "https://www.googleapis.com/auth/spreadsheets" in seen["scopes"]
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    assert not store.google_creds_available("")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/x.json")
    assert store.google_creds_available("")
