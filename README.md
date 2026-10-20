# Logo-lead outreach

Every day this finds businesses that just posted a logo contest, researches each business's **own public website**, and drafts a personalised cold email pitching website + SEO work. Sending, follow-ups, reply tracking, bounces and unsubscribes are all automatic once you turn them on. It runs on GitHub Actions, and the Google Sheet is the dashboard.

Out of the box nothing is sent: `DRY_RUN=TRUE`, `AUTO_SEND=FALSE` and `REQUIRE_APPROVAL=TRUE`.

```
daily (21:00 UTC)  inbox sync → discover → research → qualify → write drafts → draft follow-ups → summary email
send  (hourly)     inbox sync → auto-pause check → send due first emails + follow-ups (caps, windows, gaps)
```

| File | Step |
|---|---|
| `discover.py` | 3a: public DesignCrowd listing → new rows (robots.txt checked, falls back to manual) |
| `research.py` | 4–5: find website, PageSpeed + HTML audit, published email, quality score |
| `write.py` | 6: Claude writes `{subject, body}`, then code validates it |
| `send.py` | 7: Brevo send, caps, recipient-local window, random 4–9 min gaps |
| `inbox.py` | 8: Gmail replies, Brevo bounces/unsubscribes/complaints, Suppression sync, auto-pause |
| `followups.py` | 9: follow-up drafting, due dates, closing sequences out |
| `summary.py` | 10: morning summary + alerts |
| `main.py` | orchestrator / CLI |
| `config.py`, `store.py`, `web.py`, `geo.py` | settings, Sheet/JSON storage, polite fetching, location → timezone |
| `apps_script/Unsubscribe.gs` | one-click unsubscribe endpoint that writes to the Suppression tab |
| `scripts/setup_sheet.py` | creates the tabs, headers, Config defaults and the status dropdown |
| `scripts/gmail_auth.py` | one-off: gets the Gmail read-only refresh token |

## Just want a list of open jobs?

No keys or setup needed. Use **either** of these:
- **GitHub:** Actions → **outreach** → **Run workflow**, step `jobs`. When it finishes, download `designcrowd-jobs` (`jobs.csv`) from the run's page.
- **Your computer:** `pip install -r requirements.txt && python scripts/list_jobs.py`, which writes `jobs.csv`.

Once the sheet secrets are set, the same run also adds any new jobs to your sheet's **Jobs** tab. Each row has the business name (when the brief gives one), industry, location, website, job title and URL. It follows the same robots.txt and politeness rules as the full pipeline. When you write to them yourself, keep to the rules below:
- Don't mention DesignCrowd or the contest.
- Only use an address published on their own site.
- Include your address and an opt-out line.

---

## DesignCrowd terms and robots.txt (read this first)

**robots.txt decision.** I couldn't fetch `https://www.designcrowd.com/robots.txt` from the build environment because its network blocks that host. So the check runs **at the start of every daily run** instead of once:

- If robots.txt can't be read (error, 401/403/5xx), the run treats DesignCrowd as **disallowed** and uses Manual Intake Mode.
- If the listing URL is disallowed, the run uses **Manual Intake Mode**.
- Every brief URL is checked against robots.txt before it is fetched.
- The reason is written to the `Log` tab's notes column every run, e.g. `discover: robots.txt disallows the listing page -> manual intake mode`.

**Before relying on auto mode, open robots.txt yourself and check two things:**
1. The `DISCOVERY_LISTING_URL` and `DISCOVERY_BRIEF_URL_PATTERN` values in Config match the real public URLs for open **logo** projects. I couldn't load the site, so the defaults are best guesses. If they're wrong, the Log says `no brief links found`.
2. Nothing in robots.txt blocks those paths. If it does, set `DISCOVERY_MODE=manual`.

**What the crawler does and doesn't do:**
- It never logs in, uses no account, and stores no session.
- It identifies itself as `HamidBuildsLeadResearch/1.0 (+https://hamidbuilds.dev)`.
- It makes one request every 5–10 s, crawls at most 50 listing pages per run, and caches pages within a run.
- Any 403, 429 or captcha **stops the crawl for that day** and is logged. Nothing tries to get around it.
- Briefs that don't name an identifiable business (TBD, anonymous, etc.) are skipped. It never guesses.

**The No Contact clause.** DesignCrowd's terms stop *registered designers* from contacting clients off-platform. Using no account doesn't fully settle that question if you are a registered designer: the contacts still come from DesignCrowd. The safeguards below lower the risk but don't remove it:
- Emails never mention DesignCrowd, the contest or the brief. Code rejects any draft containing "designcrowd", "contest", "brief" or "logo".
- The brief URL and description are never sent to the model.
- The pitch is website/SEO only.

If you have a designer account, decide whether you're comfortable with this, or run in manual mode with leads from other sources.

## Email-law safeguards built in

- **Who gets emailed.** Only addresses published on the business's own website (on-domain, or a free-mail box clearly belonging to the business). `email_source_url` records the page each one came from.
  - No guessed addresses and no social-profile scraping.
  - Hunter is used only for `info@ / hello@ / contact@ / admin@`, and only when Hunter cites a page on the business's own domain.
- **AU Spam Act (inferred consent).** A site that says it doesn't want unsolicited or marketing emails is skipped.
- **Every email** carries your real name, `hamidbuilds.dev`, `BUSINESS_ADDRESS` and an unsubscribe link. It also sends `List-Unsubscribe` + `List-Unsubscribe-Post` headers for the Gmail/Yahoo one-click button. Sending is **blocked while `BUSINESS_ADDRESS` is empty**.
- **Opt-outs are permanent.** These all go into Suppression, which is checked at research time and again just before each send:
  - unsubscribe link clicks
  - replies containing "unsubscribe", "remove", "not interested" or "stop"
  - Brevo unsubscribes, hard bounces and spam complaints

## Safety rails

| Rail | Where |
|---|---|
| Kill switch: `AUTO_SEND=FALSE`. Config is re-read from the sheet **before every single send** | `send.run_send` |
| Hard caps: 40 new + 40 follow-ups per rolling 24 h, whatever Config says | `config.HARD_*` |
| Random 4–9 min gap between any two sends, including across runs | `send.run_send` |
| Recipient-local 9am–4pm on `SEND_DAYS` (default Tue–Thu) | `send.in_window` |
| Same domain never emailed twice within 180 days (except follow-ups in the same thread) | `research._already_contacted` |
| Auto-pause on any spam complaint, 3+ hard bounces, or bounce rate > 5% (once 10+ sends) in 24 h: sets `AUTO_SEND=FALSE` and `PAUSED_REASON`, then emails you | `inbox.health_check` |
| At most 2 follow-ups. They stop on reply, bounce or unsubscribe, and Gmail is checked again just before each follow-up | `followups.py`, `send.py` |
| `--dry-run` flag and `DRY_RUN` Config: everything except sending | `config.Settings.dry_run` |
| No secrets in logs (errors log exception type only, never request headers) | all modules |

---

## Setup

### 1. Repo
Push this folder to a **private** GitHub repo.

### 2. Google Sheet + service account
1. Create a blank Google Sheet. Copy its ID from the URL (`/d/<ID>/edit`).
2. Google Cloud Console: create a project, enable the **Google Sheets API**, then create a **Service account** with a JSON key.
3. Share the sheet with the service account's email as **Editor**.
4. Run locally (or skip this: after step 7, run the workflow with step `setup`):
   ```bash
   pip install -r requirements.txt
   cp .env.example .env   # fill in SHEET_ID and GOOGLE_SERVICE_ACCOUNT_JSON (path or one-line JSON)
   python scripts/setup_sheet.py
   ```
   This creates the `Prospects`, `Suppression`, `Config` and `Log` tabs, plus a status dropdown. The CSVs in `sheet_template/` show the same headers.
5. In **Config**, fill in `BUSINESS_ADDRESS`, `SUMMARY_TO` and `PROOF_EXAMPLE` (one real past project; leave it empty and emails cite none), and check the sender fields.

### 3. Brevo
1. Authenticate your sending domain (SPF, DKIM, DMARC) in Brevo → Senders & Domains, and verify `SENDER_EMAIL`. Send from `hamidbuilds.dev`, not a gmail address.
2. Create a v3 API key and set it as `BREVO_API_KEY`.
3. Bounces, unsubscribes and complaints are **polled** from Brevo's events API on every run, so there's no webhook to host.

### 4. Unsubscribe link (Google Apps Script, free)
1. In the sheet: **Extensions → Apps Script**, then paste `apps_script/Unsubscribe.gs`.
2. **Project Settings → Script Properties**: add `UNSUBSCRIBE_SECRET` set to a long random string. Use the same value as the GitHub secret.
3. **Deploy → New deployment → Web app**, with *Execute as: Me* and *Who has access: Anyone*. Copy the `/exec` URL into `UNSUBSCRIBE_BASE_URL`.
4. Test it with `python -c "import send,config; print(send.unsubscribe_url(config.Settings(), 'you@yourdomain.com'))"`. Open the printed link; your address should appear in the Suppression tab.
5. Optional: set `UNSUBSCRIBE_MAILTO` to an alias of your inbox (e.g. `you+unsubscribe@gmail.com`). Emails sent to it get suppressed too.

### 5. Gmail (reply detection, read-only)
1. Cloud Console: enable the **Gmail API**. Under OAuth consent screen, choose External and add yourself as a test user. Create an **OAuth client ID → Desktop app** and download it as `client_secret.json`.
2. Run `python scripts/gmail_auth.py client_secret.json` on your computer, signed in as the Reply-To inbox. It prints `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET` and `GMAIL_REFRESH_TOKEN`.
3. Publish the consent screen to "In production". Refresh tokens for apps left in "Testing" expire after 7 days.

### 6. Other keys
- **Search:** `BRAVE_API_KEY` (api.search.brave.com) **or** `SERPER_API_KEY` (serper.dev).
- **PageSpeed:** `PAGESPEED_API_KEY` (Cloud Console → PageSpeed Insights API). Free.
- **Claude:** `ANTHROPIC_API_KEY` (console.anthropic.com). The model defaults to `claude-sonnet-5-5`; override it with `CLAUDE_MODEL`.
- **Hunter (optional):** `HUNTER_API_KEY`.

### 7. GitHub secrets
Repo → Settings → Secrets and variables → Actions. Add every variable from `.env.example`. Paste the service-account JSON as the secret value.

### 8. First runs (dry-run)
1. Actions → **outreach** → *Run workflow*, with step `daily`, dry_run `true`, limit `10`.
2. Check the Prospects tab:
   - `audit_notes` should be true when you check the sites yourself.
   - `email_source_url` should really show that address.
   - `email_1_subject` / `email_1_body` should read like you.
3. Leave `DRY_RUN=TRUE` for 3–5 days of scheduled runs and review the drafts each morning.

## Turning sending on

| Goal | Config |
|---|---|
| Review every email before it goes | `DRY_RUN=FALSE`, `AUTO_SEND=TRUE`, `REQUIRE_APPROVAL=TRUE`, then set a row's status `queued` → `approved` |
| Fully automatic | as above but `REQUIRE_APPROVAL=FALSE` |
| Stop everything now | `AUTO_SEND=FALSE`. The next send attempt sees it, even mid-run |
| Resume after an auto-pause | fix the cause, clear `PAUSED_REASON`, set `AUTO_SEND=TRUE` |

You can edit a draft's subject or body in the sheet before approving. Follow-up drafts (`fu1_body`, `fu2_body`) appear about a day before they're due, so you can edit those too.

## Manual Intake Mode

Add a row to **Prospects** with `status = new` and at least `business_name`. Optional: `location`, `industry`, `website`. The next daily run researches it like any other row.
- To supply a contact yourself, fill both `contact_email` **and** `email_source_url`: the page on their own site where the address is published. Without `email_source_url`, the address is ignored and the site is searched instead.
- Set `DISCOVERY_MODE=manual` to stop the DesignCrowd crawler entirely.

## Suppression

Add a row to **Suppression** with `value` set to an email address (`jane@biz.com`) or a bare domain (`biz.com`). `type` should be `email` or `domain`; `reason` and `source` are free text. Never delete rows. Any queued or active prospect matching it is stopped on the next run.

## Status flow

`new` → `researched` → `queued` → (`approved`) → `sent` → `followup_1` → `followup_2` → `done`.

A row can also end as `skipped` (see `skip_reason`), `replied`, `bounced` or `unsubscribed`.

## Running locally

```bash
python main.py daily --dry-run --limit 10      # against the Google Sheet
python main.py daily --dry-run --local state.json   # against a local JSON file
python main.py send                            # sends only if Config allows
python -m pytest -q                            # 37 offline tests, no keys needed
```

## Cost and Actions minutes

These services fit within free tiers or a few dollars a month at about 20 emails a day:
- Brevo free tier (300 emails/day)
- PageSpeed (free)
- Brave or Serper (~$0–5)
- Claude (a few dollars)

**GitHub Actions minutes** are the one to watch on a private repo, which gets 2,000 free minutes a month:
- The send step sleeps through the 4–9 minute gaps, so 20 emails a day uses about 130 runner-minutes.
- On Tue–Thu only, that's about 1,700 minutes a month. Idle hourly runs and the daily run add a few minutes each.
- If you raise the cap or send on more days, use a $5 VPS with cron (`7 * * * 1-5 python main.py send` and `0 21 * * * python main.py daily`), or a public repo. The repo contains no secrets or lead data.

## Test-run evidence (acceptance item 5)

**Not done yet.** It needs your API keys and a reachable DesignCrowd. Once steps 1–8 are set up, run `daily` with `--dry-run --limit 10` and check the 10 drafts in the sheet. Offline coverage: `tests/` (36 tests) exercises:
- robots fallback
- brief parsing
- email filtering
- audit facts
- copy validation
- caps, gaps, windows and the kill switch
- suppression re-checks
- follow-up threading
- reply, bounce and unsubscribe handling
- auto-pause

## Acceptance checklist

- [x] robots.txt checked on every run; decision logged to `Log` and documented above (build-time fetch was blocked, see top)
- [x] No login, no DesignCrowd account, no captcha/anti-bot circumvention
- [x] Emails never mention DesignCrowd, contests or brief details (prompt + code validation)
- [x] Only published business emails; `email_source_url` recorded for every prospect that can be sent
- [x] Unsubscribe link → Suppression (Apps Script), plus mailto and reply-based opt-outs
- [x] Daily cap, sending window and random gaps (unit-tested)
- [x] Follow-ups stop on reply, bounce or unsubscribe
- [ ] Daily summary arrives (needs Brevo + `SUMMARY_TO`; check after first scheduled run)
- [ ] Dry-run verified on real prospects before `AUTO_SEND` is turned on (your 3–5 day review)
