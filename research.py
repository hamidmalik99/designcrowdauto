"""Steps 4-5: find the business website, audit it, find a PUBLISHED business email,
and score the prospect. Only verifiable facts are recorded."""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from difflib import SequenceMatcher
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import geo
from config import DOMAIN_COOLDOWN_DAYS, Settings
from store import ACTIVE_STATUSES, Store, make_id, normalize_domain, now_utc, parse_iso
from web import PoliteFetcher

log = logging.getLogger(__name__)

DIRECTORY_DOMAINS = {
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "tiktok.com", "youtube.com",
    "pinterest.com", "yelp.com", "yelp.com.au", "yellowpages.com", "yellowpages.com.au", "truelocal.com.au",
    "hotfrog.com", "hotfrog.com.au", "bbb.org", "manta.com", "crunchbase.com", "bloomberg.com",
    "opencorporates.com", "abr.business.gov.au", "companieshouse.gov.uk", "find-and-update.company-information.service.gov.uk",
    "designcrowd.com", "99designs.com", "fiverr.com", "upwork.com", "tripadvisor.com", "google.com",
    "maps.google.com", "wikipedia.org", "indeed.com", "seek.com.au", "glassdoor.com", "trustpilot.com",
    "productreview.com.au", "zoominfo.com", "dnb.com", "angi.com", "houzz.com", "hipages.com.au",
    "oneflare.com.au", "nextdoor.com", "foursquare.com", "apple.com", "amazon.com", "etsy.com",
}
SOCIAL_DOMAINS = {"facebook.com", "instagram.com", "linkedin.com", "tiktok.com", "x.com", "twitter.com"}
NAME_STOPWORDS = {"pty", "ltd", "llc", "inc", "co", "company", "the", "and", "limited", "group", "corp", "plc"}
FREE_MAIL = {"gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "yahoo.com.au", "icloud.com", "live.com",
             "bigpond.com", "bigpond.net.au", "optusnet.com.au", "aol.com", "protonmail.com", "me.com",
             "hotmail.co.uk", "btinternet.com", "xtra.co.nz"}
ROLE_LOCALS = {"info", "hello", "contact", "admin", "office", "enquiries", "enquiry", "inquiries", "sales",
               "team", "mail", "reception", "bookings", "booking", "support", "studio", "hi", "accounts"}
GENERIC_ROLE_FOR_HUNTER = {"info", "hello", "contact", "admin"}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}")
BAD_EMAIL_HINTS = ("example.", "sentry", "wixpress", "domain.com", "yourdomain", "email.com", "@2x", ".png",
                   ".jpg", ".jpeg", ".gif", ".webp", ".svg", "godaddy.com", "noreply", "no-reply", "donotreply")
NO_UNSOLICITED_RE = re.compile(
    r"(no|not accept|do not send|don't send|please do not send)\s+(any\s+)?(unsolicited|spam|marketing|"
    r"sales|commercial)\s*(e-?mails?|messages?|solicitations?|offers?|pitches)?"
    r"|unsolicited\s+(e-?mails?|sales|marketing)[^.]{0,40}(not|won't|will not)", re.I)
PARKED_MARKERS = ("domain is for sale", "this domain may be for sale", "buy this domain", "parked free",
                  "domain parking", "parkingcrew", "sedoparking", "coming soon", "under construction",
                  "website is under construction", "future home of", "launching soon", "site is coming soon")
CONTACT_HINTS = re.compile(r"contact|about|team|get-in-touch|enquir|reach-us|connect", re.I)
LOCAL_SERVICE_HINTS = re.compile(
    r"plumb|electric|build|construct|landscap|clean|cafe|coffee|restaurant|bakery|salon|beauty|barber|"
    r"dental|dentist|clinic|physio|chiro|vet|gym|fitness|yoga|pilates|real estate|realtor|account|"
    r"bookkeep|law|legal|mortgage|insurance|roof|paint|hvac|air ?con|mechanic|auto|detailing|tutor|"
    r"childcare|florist|photograph|wedding|events?|catering|brew|winery|retail|boutique|consult|"
    r"therapy|counsel|pet|groom|removal|moving|storage|pest|pool|solar|tiling|carpent|joinery|fencing", re.I)


# --- search ----------------------------------------------------------------
def web_search(cfg: Settings, query: str) -> list[dict]:
    s = cfg.secrets
    try:
        if s.brave_api_key:
            r = requests.get("https://api.search.brave.com/res/v1/web/search",
                             params={"q": query, "count": 10},
                             headers={"Accept": "application/json", "X-Subscription-Token": s.brave_api_key},
                             timeout=20)
            r.raise_for_status()
            return [{"url": x.get("url", ""), "title": x.get("title", ""), "snippet": x.get("description", "")}
                    for x in r.json().get("web", {}).get("results", [])]
        if s.serper_api_key:
            r = requests.post("https://google.serper.dev/search", json={"q": query, "num": 10},
                              headers={"X-API-KEY": s.serper_api_key}, timeout=20)
            r.raise_for_status()
            return [{"url": x.get("link", ""), "title": x.get("title", ""), "snippet": x.get("snippet", "")}
                    for x in r.json().get("organic", [])]
    except requests.RequestException as e:
        log.warning("search failed for %r: %s", query, type(e).__name__)
        return []
    raise RuntimeError("No search API key set (BRAVE_API_KEY or SERPER_API_KEY)")


def _name_tokens(name: str) -> list[str]:
    toks = re.findall(r"[a-z0-9]+", name.lower().replace("&", " and "))
    return [t for t in toks if t not in NAME_STOPWORDS]


def _root_label(domain: str) -> str:
    parts = domain.split(".")
    for suffix_len in (3, 2):  # strip things like .com.au / .co.uk
        if len(parts) > suffix_len and parts[-2] in {"com", "co", "net", "org", "gov", "edu"}:
            return parts[-3]
    return parts[0] if len(parts) <= 2 else parts[-2]


def match_confidence(business_name: str, location: str, result: dict) -> float:
    toks = _name_tokens(business_name)
    if not toks:
        return 0.0
    compact = "".join(toks)
    domain = normalize_domain(result["url"])
    label = _root_label(domain).replace("-", "")
    ratio = SequenceMatcher(None, compact, label).ratio()
    coverage = sum(1 for t in toks if t in label) / len(toks)
    conf = max(ratio, coverage * 0.9)
    title = re.sub(r"[^a-z0-9 ]", " ", result.get("title", "").lower())
    if all(t in title.split() for t in toks):
        conf += 0.2
    loc_words = [w for w in re.findall(r"[a-z]{3,}", (location or "").lower())]
    blob = (result.get("title", "") + " " + result.get("snippet", "")).lower()
    if loc_words and any(w in blob for w in loc_words):
        conf += 0.1
    return round(min(conf, 1.0), 2)


def pick_website(business_name: str, location: str, results: list[dict]) -> tuple[str, float, str]:
    """Returns (url, confidence, kind) where kind is 'website', 'social' or ''."""
    best = ("", 0.0, "")
    social = ("", 0.0, "")
    for res in results:
        domain = normalize_domain(res.get("url", ""))
        if not domain:
            continue
        base = ".".join(domain.split(".")[-2:])
        base3 = ".".join(domain.split(".")[-3:])
        if base in SOCIAL_DOMAINS or base3 in SOCIAL_DOMAINS:
            toks = _name_tokens(business_name)
            title = res.get("title", "").lower()
            if toks and all(t in title for t in toks) and social[1] == 0:
                social = (res["url"], 0.7, "social")
            continue
        if base in DIRECTORY_DOMAINS or base3 in DIRECTORY_DOMAINS or domain in DIRECTORY_DOMAINS:
            continue
        conf = match_confidence(business_name, location, res)
        if conf > best[1]:
            p = urlparse(res["url"])
            best = (f"{p.scheme}://{p.netloc}/", conf, "website")
    return best if best[1] >= 0.6 else (social if social[0] else best)


# --- site checks -------------------------------------------------------------
def is_parked(html: str) -> bool:
    text = BeautifulSoup(html, "html.parser").get_text(" ").lower()
    words = len(text.split())
    return any(m in text for m in PARKED_MARKERS) and words < 400


def pagespeed(cfg: Settings, url: str) -> dict:
    params = {"url": url, "strategy": "mobile", "category": "performance"}
    if cfg.secrets.pagespeed_api_key:
        params["key"] = cfg.secrets.pagespeed_api_key
    try:
        r = requests.get("https://www.googleapis.com/pagespeedonline/v5/runPagespeed", params=params, timeout=90)
        r.raise_for_status()
        lh = r.json().get("lighthouseResult", {})
        score = lh.get("categories", {}).get("performance", {}).get("score")
        lcp = lh.get("audits", {}).get("largest-contentful-paint", {}).get("numericValue")
        return {"score": round(score * 100) if score is not None else None,
                "lcp_s": round(lcp / 1000, 1) if lcp else None}
    except (requests.RequestException, ValueError) as e:
        log.info("PageSpeed failed for %s: %s", url, type(e).__name__)
        return {}


def detect_cms(html: str) -> str:
    h = html.lower()
    for marker, name in [("wp-content", "WordPress"), ("wix.com", "Wix"), ("squarespace", "Squarespace"),
                         ("cdn.shopify", "Shopify"), ("weebly", "Weebly"), ("godaddy", "GoDaddy Website Builder"),
                         ("webflow", "Webflow"), ("joomla", "Joomla"), ("drupal", "Drupal")]:
        if marker in h:
            return name
    return ""


def audit_site(cfg: Settings, fetcher: PoliteFetcher, url: str, html: str, final_url: str,
               https_ok: bool) -> tuple[list[str], dict]:
    """Return (plain-English findings, signals). Every finding is a checked fact."""
    soup = BeautifulSoup(html, "html.parser")
    domain = normalize_domain(final_url or url)
    notes: list[str] = []
    psi = pagespeed(cfg, final_url or url)
    if psi.get("score") is not None and psi["score"] < 50:
        notes.append(f"Google PageSpeed (mobile) performance score is {psi['score']}/100")
    if psi.get("lcp_s") and psi["lcp_s"] > 4:
        notes.append(f"main content takes about {psi['lcp_s']}s to appear on mobile (Largest Contentful Paint)")
    if not https_ok:
        notes.append("site doesn't load over HTTPS (no SSL certificate), so browsers show 'Not secure'")
    if not soup.find("meta", attrs={"name": re.compile("^viewport$", re.I)}):
        notes.append("homepage has no mobile viewport tag, so it won't scale properly on phones")
    desc = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    if not desc or not (desc.get("content") or "").strip():
        notes.append("homepage has no meta description, so Google picks its own snippet in search results")
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    if len(title) < 10:
        notes.append("homepage title tag is missing or too short to rank well")
    if not soup.find("h1"):
        notes.append("homepage has no H1 heading")
    if not soup.find("a", href=re.compile(r"^tel:", re.I)):
        notes.append("no tap-to-call phone link on the homepage")
    if not soup.find("a", href=re.compile(r"(maps\.google|goo\.gl/maps|g\.page|maps\.app\.goo\.gl|business\.google)", re.I)):
        notes.append("homepage doesn't link to a Google Business Profile / Maps listing")
    years = [int(y) for y in re.findall(r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?((?:19|20)\d{2})",
                                       html, re.I)]
    if years and max(years) < now_utc().year - 1:
        notes.append(f"footer copyright still says {max(years)}")

    internal = set()
    for a in soup.find_all("a", href=True):
        u = urljoin(final_url or url, a["href"]).split("#")[0]
        if normalize_domain(u) == domain:
            internal.add(u.rstrip("/"))
    text = soup.get_text(" ").lower()
    big_co = any(k in text for k in ("investor relations", "asx:", "nasdaq", "nyse:", "annual report"))
    signals = {"cms": detect_cms(html), "page_count_est": len(internal), "psi": psi,
               "small_business": len(internal) < 80 and not big_co}
    return notes, signals


# --- emails ------------------------------------------------------------------
def _decode_cfemail(hexstr: str) -> str:
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))
    except ValueError:
        return ""


def extract_emails(html: str) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    found = set()
    for a in soup.find_all("a", href=True):
        if a["href"].lower().startswith("mailto:"):
            found.add(a["href"][7:].split("?")[0].strip())
    for el in soup.select("[data-cfemail]"):
        found.add(_decode_cfemail(el["data-cfemail"]))
    for a in soup.find_all("a", href=re.compile(r"/cdn-cgi/l/email-protection#")):
        found.add(_decode_cfemail(a["href"].split("#")[-1]))
    found |= set(EMAIL_RE.findall(soup.get_text(" ")))
    clean = set()
    for e in found:
        e = e.strip().strip(".").lower()
        if EMAIL_RE.fullmatch(e) and not any(b in e for b in BAD_EMAIL_HINTS):
            clean.add(e)
    return clean


def email_acceptable(email: str, site_domain: str, business_name: str) -> bool:
    """Only the business's own-domain mailboxes, or a free-mail box that is clearly
    the business's (role or business-name local part) shown on its own site."""
    local, _, dom = email.partition("@")
    if dom == site_domain or dom.endswith("." + site_domain):
        return True
    if dom in FREE_MAIL:
        toks = _name_tokens(business_name)
        compact_local = re.sub(r"[^a-z0-9]", "", local)
        return local in ROLE_LOCALS or (toks and "".join(toks)[:6] in compact_local)
    return False


def contact_name_for(email: str, page_text: str) -> str:
    local = email.split("@")[0]
    if local in ROLE_LOCALS:
        return ""
    parts = [p for p in re.split(r"[._-]", local) if p.isalpha() and len(p) > 1]
    if not parts or len(parts) > 2:
        return ""
    first = parts[0].capitalize()
    if not re.search(rf"\b{re.escape(first)}\b", page_text):
        return ""
    if len(parts) == 2:
        full = f"{first} {parts[1].capitalize()}"
        return full if re.search(rf"\b{re.escape(full)}\b", page_text) else first
    return first


def find_contact(fetcher: PoliteFetcher, base_url: str, home_html: str, site_domain: str,
                 business_name: str) -> dict:
    """Search homepage + contact/about pages. Returns {} or
    {email, name, source_url, no_unsolicited}."""
    pages = [(base_url, home_html)]
    soup = BeautifulSoup(home_html, "html.parser")
    candidates = []
    for a in soup.find_all("a", href=True):
        u = urljoin(base_url, a["href"]).split("#")[0]
        if normalize_domain(u) == site_domain and (CONTACT_HINTS.search(a["href"]) or
                                                   CONTACT_HINTS.search(a.get_text(" "))):
            candidates.append(u)
    candidates += [urljoin(base_url, p) for p in ("/contact", "/contact-us", "/about", "/about-us")]
    seen = {base_url.rstrip("/")}
    for u in candidates:
        if u.rstrip("/") in seen or len(pages) >= 6:
            continue
        seen.add(u.rstrip("/"))
        r = fetcher.get(u)
        if r is not None and "html" in r.headers.get("Content-Type", "html"):
            pages.append((r.url, r.text))

    best, no_unsolicited = None, False
    for url, html in pages:
        text = BeautifulSoup(html, "html.parser").get_text(" ")
        if NO_UNSOLICITED_RE.search(text):
            no_unsolicited = True
        for e in sorted(extract_emails(html)):
            if not email_acceptable(e, site_domain, business_name):
                continue
            name = contact_name_for(e, text)
            on_domain = e.endswith("@" + site_domain) or e.split("@")[1].endswith("." + site_domain)
            rank = (2 if on_domain else 0) + (1 if name else 0)
            if best is None or rank > best["rank"]:
                best = {"email": e, "name": name, "source_url": url, "rank": rank}
    if not best:
        return {"no_unsolicited": no_unsolicited}
    best.pop("rank")
    best["no_unsolicited"] = no_unsolicited
    return best


def hunter_generic(cfg: Settings, domain: str) -> dict:
    """Hunter fallback: generic role addresses only, and only if Hunter cites a
    source page on the business's own domain (proof it was published there)."""
    if not cfg.secrets.hunter_api_key:
        return {}
    try:
        r = requests.get("https://api.hunter.io/v2/domain-search",
                         params={"domain": domain, "type": "generic", "api_key": cfg.secrets.hunter_api_key},
                         timeout=20)
        r.raise_for_status()
    except requests.RequestException as e:
        log.info("hunter failed for %s: %s", domain, type(e).__name__)
        return {}
    for em in r.json().get("data", {}).get("emails", []):
        value = (em.get("value") or "").lower()
        if value.split("@")[0] not in GENERIC_ROLE_FOR_HUNTER or not value.endswith("@" + domain):
            continue
        for src in em.get("sources", []):
            uri = src.get("uri", "")
            if normalize_domain(uri) == domain or normalize_domain(uri).endswith("." + domain):
                return {"email": value, "name": "", "source_url": uri}
    return {}


# --- qualify -----------------------------------------------------------------
def quality_score(cfg: Settings, country: str, has_website: str, audit_notes: list[str],
                  small_business: bool, industry: str, contact_name: str) -> int:
    score = 0
    if country in cfg.target_countries:
        score += 3
    if has_website != "yes" or audit_notes:
        score += 3
    if small_business or LOCAL_SERVICE_HINTS.search(industry or ""):
        score += 2
    if contact_name:
        score += 2
    return score


# --- orchestration -----------------------------------------------------------
def _already_contacted(store: Store, row: dict, pid: str, domain: str) -> str:
    cutoff = now_utc() - timedelta(days=DOMAIN_COOLDOWN_DAYS)
    for other in store.prospects():
        if other is row or (other.get("id") != pid and other.get("domain") != domain):
            continue
        sent = parse_iso(other.get("sent_at", ""))
        if sent and sent > cutoff:
            return "already contacted in last 6 months"
        if other.get("status") in ACTIVE_STATUSES | {"queued", "approved", "researched"}:
            return "duplicate of an existing prospect"
    return ""


def _fetch_home(fetcher: PoliteFetcher, website: str) -> tuple[object, bool]:
    domain = normalize_domain(website)
    r = fetcher.get(f"https://{domain}/") or fetcher.get(f"https://www.{domain}/")
    if r is not None:
        return r, True
    r = fetcher.get(f"http://{domain}/") or fetcher.get(f"http://www.{domain}/")
    return r, False


def research_prospect(store: Store, cfg: Settings, row: dict, fetcher: PoliteFetcher) -> str:
    """Research one row. Returns the new status."""
    def skip(reason: str, **extra) -> str:
        store.update_prospect(row, status="skipped", skip_reason=reason, **extra)
        return "skipped"

    name, location = row["business_name"].strip(), row.get("location", "")
    if not name:
        return skip("no business identified")

    website, confidence, kind = row.get("website", "").strip(), 1.0, "website"
    if not website:
        results = web_search(cfg, f'"{name}" {location}'.strip())
        if row.get("industry"):
            results += web_search(cfg, f'"{name}" {row["industry"]}')
        website, confidence, kind = pick_website(name, location, results)
    if kind == "social":
        return skip("social-only presence, no website or published email", has_website="social-only",
                    search_confidence=confidence)
    if not website or confidence < 0.6:
        return skip("could not confidently identify the business website", has_website="no",
                    search_confidence=confidence)

    domain = normalize_domain(website)
    pid = make_id(domain)
    store.update_prospect(row, website=website, domain=domain, id=pid, search_confidence=confidence)
    if store.is_suppressed(domain=domain):
        return skip("domain is on the suppression list")
    dup = _already_contacted(store, row, pid, domain)
    if dup:
        return skip(dup)

    resp, https_ok = _fetch_home(fetcher, website)
    if resp is None:
        return skip("website unreachable or robots.txt disallows it", has_website="no")
    html, final_url = resp.text, resp.url
    if is_parked(html):
        return skip("website is parked / under construction, no published email", has_website="parked")

    notes, signals = audit_site(cfg, fetcher, website, html, final_url, https_ok)
    country, tz = geo.resolve(location, domain)
    store.update_prospect(row, has_website="yes", timezone=tz,
                          audit_notes="\n".join(f"- {n}" for n in notes[:4]))

    if row.get("contact_email") and row.get("email_source_url"):
        contact = {"email": row["contact_email"].lower(), "name": row.get("contact_name", ""),
                   "source_url": row["email_source_url"], "no_unsolicited": False}
    else:
        contact = find_contact(fetcher, final_url, html, domain, name)
        if not contact.get("email"):
            contact.update(hunter_generic(cfg, domain))
    if contact.get("no_unsolicited"):
        return skip("site says it does not accept unsolicited emails")
    if not contact.get("email"):
        return skip("no published email")
    if store.is_suppressed(email=contact["email"]):
        return skip("email is on the suppression list")
    if not tz:
        return skip("location/timezone unknown, can't respect sending window",
                    contact_email=contact["email"], email_source_url=contact["source_url"])

    score = quality_score(cfg, country, "yes", notes, signals["small_business"], row.get("industry", ""),
                          contact.get("name", ""))
    store.update_prospect(row, contact_email=contact["email"], contact_name=contact.get("name", ""),
                          email_source_url=contact["source_url"], quality_score=score)
    if score < cfg.min_quality_score:
        return skip(f"quality score {score} below minimum {cfg.min_quality_score}")
    store.update_prospect(row, status="researched", skip_reason="")
    return "researched"


def run_research(store: Store, cfg: Settings, limit: int | None = None,
                 fetcher: PoliteFetcher | None = None) -> dict:
    fetcher = fetcher or PoliteFetcher(cfg.secrets.bot_info_url)
    stats = {"researched": 0, "skipped": 0, "errors": 0, "skip_reasons": {}}
    todo = [r for r in store.prospects() if r.get("status") == "new"][: limit or cfg.research_per_run]
    for row in todo:
        try:
            status = research_prospect(store, cfg, row, fetcher)
        except Exception as e:  # one bad site must not stop the run
            log.exception("research failed for row %s", row.get("_row"))
            store.update_prospect(row, last_error=f"research: {type(e).__name__}: {str(e)[:150]}")
            stats["errors"] += 1
            continue
        if status == "skipped":
            stats["skipped"] += 1
            reason = row.get("skip_reason", "")
            stats["skip_reasons"][reason] = stats["skip_reasons"].get(reason, 0) + 1
        else:
            stats["researched"] += 1
        store.flush()
    return stats
