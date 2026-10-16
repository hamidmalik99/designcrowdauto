"""Turn a free-text location (and/or a website's TLD) into a country code and IANA timezone."""
from __future__ import annotations

import re

COUNTRY_ALIASES = {
    "AU": ["australia", "aus"],
    "UK": ["united kingdom", "uk", "england", "scotland", "wales", "northern ireland", "great britain", "gb"],
    "US": ["united states", "usa", "us", "u.s.", "u.s.a.", "america"],
    "NZ": ["new zealand", "nz", "aotearoa"],
    "CA": ["canada"],
    "IE": ["ireland"],
}
COUNTRY_DEFAULT_TZ = {"AU": "Australia/Sydney", "UK": "Europe/London", "US": "America/New_York",
                      "NZ": "Pacific/Auckland", "CA": "America/Toronto", "IE": "Europe/Dublin"}

AU_REGIONS = {
    "Australia/Sydney": ["nsw", "new south wales", "sydney", "newcastle", "wollongong", "act", "canberra",
                         "vic", "victoria", "melbourne", "geelong", "tas", "tasmania", "hobart"],
    "Australia/Brisbane": ["qld", "queensland", "brisbane", "gold coast", "sunshine coast", "cairns", "townsville"],
    "Australia/Adelaide": ["sa", "south australia", "adelaide"],
    "Australia/Perth": ["wa", "western australia", "perth"],
    "Australia/Darwin": ["nt", "northern territory", "darwin"],
}
CA_REGIONS = {
    "America/Vancouver": ["bc", "british columbia", "vancouver", "victoria bc"],
    "America/Edmonton": ["ab", "alberta", "calgary", "edmonton"],
    "America/Regina": ["sk", "saskatchewan"],
    "America/Winnipeg": ["mb", "manitoba", "winnipeg"],
    "America/Toronto": ["on", "ontario", "toronto", "ottawa", "qc", "quebec", "montreal"],
    "America/Halifax": ["ns", "nova scotia", "nb", "new brunswick", "pe", "prince edward island", "halifax"],
    "America/St_Johns": ["nl", "newfoundland"],
}
US_STATES = {  # abbreviation: (name, timezone)
    "AL": ("alabama", "America/Chicago"), "AK": ("alaska", "America/Anchorage"),
    "AZ": ("arizona", "America/Phoenix"), "AR": ("arkansas", "America/Chicago"),
    "CA": ("california", "America/Los_Angeles"), "CO": ("colorado", "America/Denver"),
    "CT": ("connecticut", "America/New_York"), "DE": ("delaware", "America/New_York"),
    "FL": ("florida", "America/New_York"), "GA": ("georgia", "America/New_York"),
    "HI": ("hawaii", "Pacific/Honolulu"), "ID": ("idaho", "America/Boise"),
    "IL": ("illinois", "America/Chicago"), "IN": ("indiana", "America/Indiana/Indianapolis"),
    "IA": ("iowa", "America/Chicago"), "KS": ("kansas", "America/Chicago"),
    "KY": ("kentucky", "America/New_York"), "LA": ("louisiana", "America/Chicago"),
    "ME": ("maine", "America/New_York"), "MD": ("maryland", "America/New_York"),
    "MA": ("massachusetts", "America/New_York"), "MI": ("michigan", "America/Detroit"),
    "MN": ("minnesota", "America/Chicago"), "MS": ("mississippi", "America/Chicago"),
    "MO": ("missouri", "America/Chicago"), "MT": ("montana", "America/Denver"),
    "NE": ("nebraska", "America/Chicago"), "NV": ("nevada", "America/Los_Angeles"),
    "NH": ("new hampshire", "America/New_York"), "NJ": ("new jersey", "America/New_York"),
    "NM": ("new mexico", "America/Denver"), "NY": ("new york", "America/New_York"),
    "NC": ("north carolina", "America/New_York"), "ND": ("north dakota", "America/Chicago"),
    "OH": ("ohio", "America/New_York"), "OK": ("oklahoma", "America/Chicago"),
    "OR": ("oregon", "America/Los_Angeles"), "PA": ("pennsylvania", "America/New_York"),
    "RI": ("rhode island", "America/New_York"), "SC": ("south carolina", "America/New_York"),
    "SD": ("south dakota", "America/Chicago"), "TN": ("tennessee", "America/Chicago"),
    "TX": ("texas", "America/Chicago"), "UT": ("utah", "America/Denver"),
    "VT": ("vermont", "America/New_York"), "VA": ("virginia", "America/New_York"),
    "WA": ("washington", "America/Los_Angeles"), "WV": ("west virginia", "America/New_York"),
    "WI": ("wisconsin", "America/Chicago"), "WY": ("wyoming", "America/Denver"),
    "DC": ("district of columbia", "America/New_York"),
}
NZ_CITIES = ["auckland", "wellington", "christchurch", "hamilton", "tauranga", "dunedin", "queenstown"]
UK_CITIES = ["london", "manchester", "birmingham", "leeds", "glasgow", "edinburgh", "bristol",
             "liverpool", "cardiff", "belfast", "sheffield", "nottingham", "leicester"]
TLD_COUNTRY = {".com.au": "AU", ".net.au": "AU", ".au": "AU", ".co.uk": "UK", ".org.uk": "UK",
               ".uk": "UK", ".co.nz": "NZ", ".nz": "NZ", ".ca": "CA", ".ie": "IE", ".us": "US"}


def _has(text: str, term: str) -> bool:
    return re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", text) is not None


def resolve(location: str = "", domain: str = "") -> tuple[str, str]:
    """Return (country_code, timezone). Empty strings when unknown."""
    raw = location or ""
    text = raw.lower()
    country = ""
    for code, names in COUNTRY_ALIASES.items():
        if any(_has(text, n) for n in names):
            country = code
            break

    if country in ("", "AU"):
        ambiguous = {"wa", "sa", "nt"}  # also US state / other abbreviations
        for tz, names in AU_REGIONS.items():
            if any(_has(text, n) for n in names if len(n) > 3) or any(
                    re.search(rf"\b{n.upper()}\b", raw) for n in names
                    if len(n) <= 3 and (n not in ambiguous or country == "AU")):
                return "AU", tz
    if country in ("", "CA"):
        for tz, names in CA_REGIONS.items():
            if any(_has(text, n) for n in names if len(n) > 2) or (
                    country == "CA" and any(re.search(rf"\b{n.upper()}\b", raw) for n in names if len(n) <= 2)):
                return "CA", tz
    if country in ("", "US"):
        for abbr, (name, tz) in US_STATES.items():
            if _has(text, name) or re.search(rf",\s*{abbr}\b", raw):
                return "US", tz
    if country in ("", "NZ") and any(_has(text, c) for c in NZ_CITIES):
        return "NZ", COUNTRY_DEFAULT_TZ["NZ"]
    if country in ("", "UK") and any(_has(text, c) for c in UK_CITIES):
        return "UK", COUNTRY_DEFAULT_TZ["UK"]

    if not country and domain:
        d = domain.lower()
        for tld, code in sorted(TLD_COUNTRY.items(), key=lambda kv: -len(kv[0])):
            if d.endswith(tld):
                country = code
                break
    return country, COUNTRY_DEFAULT_TZ.get(country, "")
