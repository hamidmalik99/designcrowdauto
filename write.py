"""Step 6 (and follow-up copy): write personalised emails with the Claude API,
then validate them in code. The model never sees the contest brief."""
from __future__ import annotations

import json
import logging
import re

import anthropic

from config import Settings
from store import Store

log = logging.getLogger(__name__)

BANNED_TERMS = ["designcrowd", "design crowd", "contest", "brief", "logo", "crowdsourc"]
HYPE_WORDS = ["revolutionize", "revolutionise", "skyrocket", "game-changer", "game changer", "supercharge",
              "10x", "guaranteed", "explode", "dominate", "unleash", "cutting-edge", "synergy"]
PLACEHOLDER_RE = re.compile(r"\{[^}]*\}|\[[^\]]*\]|<[^>]+>|\bXX+\b|lorem ipsum|\bTODO\b|\bINSERT\b", re.I)
URL_RE = re.compile(r"(https?://\S+|www\.\S+|\b[a-z0-9-]+\.(?:dev|com|net|org|io|co|au|uk)\b\S*)", re.I)

EMAIL_SCHEMA = {
    "type": "object",
    "properties": {"subject": {"type": "string"}, "body": {"type": "string"}},
    "required": ["subject", "body"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You write short, honest cold emails for {sender_name} ("{first_name}"), a freelance \
WordPress developer and SEO specialist (5+ years WordPress/WooCommerce, white-label work for agencies in \
Australia, the UK, the US and New Zealand). You pitch website and SEO services only.

Hard rules:
- Use ONLY the facts given in the prospect data. Never invent findings, numbers, clients, results or familiarity.
- Never mention logos, design contests, design marketplaces, briefs, or how the business was found.
- Never say anything that implies you know about their branding plans.
- Plain text. No links or URLs at all (a signature with the portfolio link is added automatically).
- No sign-off or signature: end the body with the closing question.
- No hype words (revolutionize, skyrocket, game-changer, supercharge, guaranteed, etc.). Human, plain, warm tone.
- Australian/British spelling unless the prospect is in the US or Canada."""

FIRST_EMAIL_PROMPT = """Write the first email.

Structure:
1. Greeting: "Hi {{contact_name}}," if a contact name is given, otherwise "Hi there,".
2. Opener: one specific, true observation about THEIR website taken from the audit findings below \
(or, if they have no website, the fact that they don't have one yet). Phrase it helpfully, not as criticism.
3. Value: what a fast, modern WordPress site plus local SEO would do for a {industry_or_business} like theirs.
4. Proof: one short paragraph - 5+ years WordPress/WooCommerce, white-label work for agencies in AU/UK/US/NZ, \
and this example: {proof_example}.
5. Close: one low-friction question, e.g. offering a free 3-point audit of their site or a quick mockup idea.

Body length: 120-160 words (excluding nothing - count every word of the body).
Subject: short, mostly lowercase, specific, e.g. "quick idea for {business_name} website". No clickbait.

Prospect data:
{prospect}"""

FOLLOWUP_PROMPTS = {
    1: """Write follow-up #1, sent in the same thread {days} days after the first email (shown below).
- 40-70 words. Start with "Hi {{name}}," or "Hi there," like the original.
- Add ONE new, specific, true point from the audit findings that the first email did not use \
(if none is left, a concrete benefit of fixing the first point).
- Re-offer the same low-friction close. No guilt-tripping ("just bumping this").
Return the body only in "body"; set "subject" to an empty string.

First email:
{first_email}

Prospect data:
{prospect}""",
    2: """Write follow-up #2 (final), sent in the same thread. Previous emails are shown below.
- 30-50 words. Polite close-the-loop: say this is the last note, the offer stands, no reply needed if the \
timing's wrong. Start with "Hi {{name}}," or "Hi there,".
Return the body only in "body"; set "subject" to an empty string.

Previous emails:
{first_email}

Prospect data:
{prospect}""",
}
WORD_RANGES = {0: (120, 160), 1: (40, 70), 2: (30, 50)}


def word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'’-]+\b", text))


def validate(subject: str, body: str, kind: int = 0) -> list[str]:
    """Return a list of problems (empty = valid). kind 0 = first email, 1/2 = follow-ups."""
    problems = []
    lo, hi = WORD_RANGES[kind]
    n = word_count(body)
    if not lo <= n <= hi:
        problems.append(f"word count {n} not in {lo}-{hi}")
    blob = f"{subject}\n{body}".lower()
    for t in BANNED_TERMS:
        if t in blob:
            problems.append(f"banned term '{t}'")
    for t in HYPE_WORDS:
        if t in blob:
            problems.append(f"hype word '{t}'")
    if PLACEHOLDER_RE.search(subject + "\n" + body):
        problems.append("unfilled placeholder")
    if len(URL_RE.findall(body)) > 1:
        problems.append("more than one link")
    if kind == 0:
        if not subject.strip():
            problems.append("empty subject")
        if len(subject) > 70:
            problems.append("subject too long")
    return problems


def prospect_facts(row: dict) -> str:
    """Only these fields reach the model - never source_url or brief text."""
    facts = {
        "business_name": row["business_name"],
        "industry": row.get("industry") or "unknown",
        "location": row.get("location") or "unknown",
        "website": row.get("website") or "none",
        "website_status": row.get("has_website") or "unknown",
        "audit_findings": row.get("audit_notes") or "none recorded",
        "contact_name": row.get("contact_name") or "",
    }
    return json.dumps(facts, indent=2)


def claude_json(cfg: Settings, system: str, prompt: str) -> dict:
    client = anthropic.Anthropic(api_key=cfg.secrets.anthropic_api_key or None)
    resp = client.messages.create(
        model=cfg.secrets.claude_model,
        max_tokens=8000,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": EMAIL_SCHEMA}},
        # Server-side refusal fallback (Claude API): re-runs a declined request on a suitable model.
        extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
        extra_body={"fallbacks": "default"},
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("model declined the request")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("model output truncated")
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


def _system(cfg: Settings) -> str:
    return SYSTEM_PROMPT.format(sender_name=cfg.get("SENDER_NAME"), first_name=cfg.get("SENDER_FIRST_NAME"))


def generate(cfg: Settings, prompt: str, kind: int, generator=None) -> tuple[dict | None, list[str]]:
    """Generate + validate; regenerate once with the problems listed. Returns (email, problems)."""
    gen = generator or (lambda p: claude_json(cfg, _system(cfg), p))
    problems: list[str] = []
    for attempt in range(2):
        p = prompt if attempt == 0 else (
            prompt + "\n\nYour previous draft failed these checks, fix them: " + "; ".join(problems))
        out = gen(p)
        subject, body = (out.get("subject") or "").strip(), (out.get("body") or "").strip()
        problems = validate(subject, body, kind)
        if not problems:
            return {"subject": subject, "body": body}, []
    return None, problems


def write_first_email(cfg: Settings, row: dict, generator=None) -> tuple[dict | None, list[str]]:
    industry = row.get("industry") or ""
    prompt = FIRST_EMAIL_PROMPT.format(
        industry_or_business=industry.lower() if industry else "business",
        proof_example=cfg.get("PROOF_EXAMPLE"),
        business_name=row["business_name"],
        prospect=prospect_facts(row),
    )
    return generate(cfg, prompt, 0, generator)


def write_followup(cfg: Settings, row: dict, n: int, generator=None) -> tuple[dict | None, list[str]]:
    first = f"Subject: {row['email_1_subject']}\n\n{row['email_1_body']}"
    if n == 2 and row.get("fu1_body"):
        first += f"\n\n--- follow-up 1 ---\n{row['fu1_body']}"
    prompt = FOLLOWUP_PROMPTS[n].format(days=cfg.followup_1_days, first_email=first,
                                        prospect=prospect_facts(row))
    return generate(cfg, prompt, n, generator)


def run_write(store: Store, cfg: Settings, generator=None) -> dict:
    stats = {"queued": 0, "skipped": 0, "errors": 0, "skip_reasons": {}}
    for row in [r for r in store.prospects() if r.get("status") == "researched"]:
        try:
            email, problems = write_first_email(cfg, row, generator)
        except Exception as e:
            log.exception("writing failed for row %s", row.get("_row"))
            store.update_prospect(row, last_error=f"write: {type(e).__name__}: {str(e)[:150]}")
            stats["errors"] += 1
            continue
        if email is None:
            store.update_prospect(row, status="skipped", skip_reason="copy failed validation",
                                  last_error="; ".join(problems)[:300])
            stats["skipped"] += 1
            stats["skip_reasons"]["copy failed validation"] = stats["skip_reasons"].get("copy failed validation", 0) + 1
        else:
            store.update_prospect(row, status="queued", email_1_subject=email["subject"],
                                  email_1_body=email["body"], last_error="")
            stats["queued"] += 1
        store.flush()
    return stats
