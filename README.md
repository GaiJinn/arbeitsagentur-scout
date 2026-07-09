# 🎯 arbeitsagentur-scout

> Personal job-hunting bot for the German federal employment agency
> ([arbeitsagentur.de](https://www.arbeitsagentur.de/)) — automated search,
> LLM-based ranking, deduplication, Telegram alerts.

[![CI](https://github.com/GaiJinn/arbeitsagentur-scout/actions/workflows/ci.yml/badge.svg)](https://github.com/GaiJinn/arbeitsagentur-scout/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.12-blue)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED)](https://www.docker.com/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

---

## What this does

Every 4 hours (via cron) the scout:

1. Hits the (community-documented) Bundesagentur für Arbeit Jobsuche REST API
   for each of your configured search queries.
2. Stores every hit in a local SQLite db keyed on `refnr` so you only see each
   posting once.
3. For brand-new postings, fetches the full Stellenbeschreibung and asks
   Llama 3.3 70B (via Groq) to score it 1–10 against your candidate profile,
   summarise the fit in one sentence, and flag concerns.
4. Pushes a single Telegram message with everything that scored at or above
   your threshold (default 6/10).
5. For jobs scoring at or above a second, higher threshold (default 7/10),
   sends a separate message with a "📄 CV generieren" button — tap it and a
   companion always-on service tailors your base CV to that job and sends
   the result back as a PDF. See [Auto-generate tailored CVs](#auto-generate-tailored-cvs).

It's a cron-friendly one-shot script — run it, it does its job, exits. The
CV-generation button is the one part that needs a small always-on listener
alongside it (see below).

## Why I built it

Manuelles Durchsuchen mehrerer Jobportale jeden Tag kostet 30+ Minuten.
arbeitsagentur ist die einzige Plattform, die wirklich vollständig ist
(Mittelstand, öffentlicher Dienst, Konzerne — alle inserieren dort), aber
die UI sieht aus wie 2008. Mit einem schmalen Wrapper plus LLM-Vorqualifizierung
sind aus 30 Minuten "Tab-Hopping" 2 Minuten "Telegram lesen".

## Architecture

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  Cron / VPS  │ ─▶  │  scout.py    │ ─▶  │   Groq LLM   │
└──────────────┘     │  (one-shot)  │     │  (Llama 3.3) │
                     └──────┬───────┘     └──────┬───────┘
                            │                    │
                            ▼                    ▼
       ┌─────────────────────────┐    ┌────────────────────┐
       │ arbeitsagentur REST API │    │ Telegram Bot API   │
       └─────────────────────────┘    └─────────┬──────────┘
                            │                    │ "📄 CV generieren" tap
                            ▼                    ▼
                     ┌─────────────┐    ┌──────────────────┐
                     │  SQLite     │◀───│  telegram_bot.py  │
                     │  (dedup,    │    │  (long-running,   │
                     │  job cache) │    │  listens for      │
                     └─────────────┘    │  button clicks)   │
                                         └─────────┬──────────┘
                                                   │
                                                   ▼
                                        cv_generator.py: base CV
                                        (PDF) + job description
                                        → Groq LLM → tailored PDF
```

## Tech stack

- **Python 3.12** — `httpx` for HTTP, `groq` for LLM, stdlib `sqlite3`
- **Groq Cloud** with `llama-3.3-70b-versatile` (free tier covers daily use)
- **Telegram Bot API** for notifications and the CV-generation button
- **pypdf** / **reportlab** to extract and re-render CVs as PDF
- **Docker** for clean, reproducible deployment, with a heartbeat-based
  `HEALTHCHECK` on the always-on `bot` service
- **Streamlit** (optional) for a read-only dashboard over the job history
- **Bundesagentur für Arbeit Jobsuche API** (community-documented at
  [bundesAPI/jobsuche-api](https://github.com/bundesAPI/jobsuche-api)) via a
  `JobSource` interface designed to let a second portal plug in later

### Resilience

- All arbeitsagentur HTTP calls retry with exponential backoff on 5xx/network
  errors; 4xx fails fast (see `arbeitsagentur.py`).
- All Groq calls (`llm_utils.py`, shared by `analyzer.py` and
  `cv_generator.py`) retry on 429 rate limits (honoring `Retry-After` when
  sent) and re-prompt the model up to twice if it returns malformed JSON.
- If the full Stellenbeschreibung can't be fetched for a job, that job is
  saved **unscored** rather than scored on just its title — a thin fallback
  produces an unreliable LLM score, so scout.py skips scoring instead of
  guessing.
- Every log line is tagged with a short id: a `run_id` per `scout.py` cron
  run, a `request_id` per `telegram_bot.py` callback handled — useful for
  grepping one run's/request's output out of a shared log file.

## Quick start

### 1. Clone and configure

```bash
git clone https://github.com/GaiJinn/arbeitsagentur-scout.git
cd arbeitsagentur-scout
cp .env.example .env
$EDITOR .env
cp profile.example.md profile.md
$EDITOR profile.md
cp queries.example.json queries.json
$EDITOR queries.json
```

`profile.md` and `queries.json` are gitignored — they hold your personal
background and search preferences and never get committed.

You need:

- `GROQ_API_KEY` → free at [console.groq.com](https://console.groq.com/keys)
- `TELEGRAM_TOKEN` → ask `@BotFather` on Telegram, `/newbot`
- `TELEGRAM_CHAT_ID` → see "Get your Telegram chat ID" below

Optional, for tailored-CV generation: drop your base CV at `cv.pdf` (path
configurable via `BASE_CV_PATH`). See [Auto-generate tailored CVs](#auto-generate-tailored-cvs).

### 2. Tune your queries

Edit `queries.json`. Each entry is one arbeitsagentur search call, with the
API params:

| param                | meaning                                                 |
| -------------------- | ------------------------------------------------------- |
| `was`                | keywords (title / description)                          |
| `wo`                 | city / postcode / region                                |
| `umkreis`            | radius in km (0/10/25/50/100/200)                       |
| `veroeffentlichtseit`| only postings of the last N days                        |

Each entry also accepts an optional top-level `"source"` field (default:
`"arbeitsagentur"`) naming which `JobSource` to run that query against —
`"greenhouse"`, `"lever"`, and `"personio"` are also built in, for watching
specific companies' career pages directly. See
[Watching specific companies](#watching-specific-companies).

You can also edit `profile.md` to match your own background — that's what
the LLM scores against.

### 3. Run locally to test

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
DB_PATH=./jobs.db python scout.py
```

First run will pull a lot of jobs (everything is "new"). Subsequent runs
only see truly new postings.

### 4. Run the test suite

```bash
pip install -r requirements-dev.txt
pytest -v
```

Tests mock the arbeitsagentur and Groq APIs (no real network calls or API
keys needed) and cover pagination, retry/backoff, JSON parsing, SQLite
dedup, and the dashboard's filtering logic. `tests/test_integration.py` drives
`scout.main()` end-to-end over mocked HTTP (search → score → dedup →
Telegram alert) to catch wiring mistakes the per-module unit tests can't. CI
runs the whole suite on every push via [GitHub Actions](.github/workflows/ci.yml).

### 5. Deploy on a VPS, cron every 4 hours

```bash
# on your VPS
git clone https://github.com/GaiJinn/arbeitsagentur-scout.git
cd arbeitsagentur-scout
cp .env.example .env && $EDITOR .env
cp profile.example.md profile.md && $EDITOR profile.md
cp queries.example.json queries.json && $EDITOR queries.json
docker compose build

# Add to crontab
crontab -e
```

Add:

```cron
0 */4 * * * cd /opt/arbeitsagentur-scout && /usr/bin/docker compose run --rm scout >> /var/log/scout.log 2>&1
```

If you want the CV-generation button, also start the always-on listener once:

```bash
docker compose up -d bot
```

`docker compose ps` will show `bot` as `healthy`/`unhealthy` based on a
heartbeat file it writes once per poll cycle (~30s) — useful for noticing a
silently-hung long-poll loop that `restart: unless-stopped` alone wouldn't
catch (the process isn't crashed, just stuck).

## Browse job history (dashboard)

A read-only Streamlit page over the same `jobs.db` scout.py writes to —
filter by score/employer/location, see the LLM's summary/flags/skills per
job, open the original listing. Never writes to the db, safe to run
alongside a live cron job / bot.

```bash
pip install -r requirements-dashboard.txt
streamlit run dashboard.py
# or, to point at a specific db:
DB_PATH=./data/jobs.db streamlit run dashboard.py
```

Kept as a separate `requirements-dashboard.txt` (Streamlit + pandas) so the
cron/bot production footprint doesn't grow just to get a browsing UI.

## Sync to Notion

Optionally, every new job also gets mirrored into a Notion database — one
row per job (Title, Employer, Location, Score, Source, Key Skills, Flags,
Posted Date, Seen At, URL), with the full LLM summary + Stellenbeschreibung
as the page body. Click any row to read the details; use Notion's own
grouped/board views for the aggregate look (see below) — no separate
"stats page" to build or keep in sync.

This talks to Notion's REST API directly (`notion_sync.py`, same style as
`notifier.py`'s Telegram client), not through any chat-based Notion
connector — `scout.py` runs unattended via cron, so it needs its own
long-lived credential, not something tied to an interactive session.

### 1. Create a Notion integration

1. Go to [notion.so/my-integrations](https://www.notion.so/my-integrations) → **New integration**.
2. Give it a name (e.g. "arbeitsagentur-scout"), pick your workspace, save.
3. Copy the **Internal Integration Token** → this is `NOTION_API_KEY`.

### 2. Share a page with it

1. In Notion, create (or pick) a page that will hold the "Job Scout"
   database — a fresh empty page is cleanest.
2. Open it, click `···` → **Connections** (or **Add connections**) → select
   the integration you just created.
3. Copy the page's id from its URL: `notion.so/My-Page-<32-hex-chars>` — the
   32 hex characters (no dashes needed) are `NOTION_PARENT_PAGE_ID`.

### 3. Configure and run

Set both in `.env`:

```bash
NOTION_API_KEY=secret_xxx...
NOTION_PARENT_PAGE_ID=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

The "Job Scout" database is created automatically under that page on the
first run after this is configured — nothing to set up by hand in Notion
beyond sharing the parent page. Its id (and each synced job's page id) is
cached in the shared `jobs.db` so re-runs never create duplicate rows.

### Getting the "汇总" / grouped overview

Once the database exists (after the first run with Notion configured), open
it in Notion and add a view grouped the way you want — this is a one-time,
no-code step Notion already does well, so `notion_sync.py` doesn't try to
duplicate it via the API:

- **Board view, grouped by Employer or Source** — see companies/portals at a
  glance, click into any card for full details.
- **Table view, grouped by Score** — click the "..." menu → Group → Score to
  see counts per score band.
- **Sort by Seen At descending** — a simple "what's new" feed.
- **Board/table view, grouped by City** — the per-metro trend view. City is
  a multi-select fed by each query's `"region"`: multi-location postings
  (one refnr offered in several cities — consultancies, agencies, remote
  roles) are created by whichever query sees them first and gain the other
  cities as later queries re-see the same refnr, so e.g. a Berlin group
  fills in even when every Berlin hit is also posted in Düsseldorf/München.

A Notion outage or misconfiguration never affects the actual job search /
scoring / Telegram alert — sync failures are logged and swallowed, not
raised (see `notion_sync.py`'s `sync_new_jobs`).

## Watching specific companies

Besides arbeitsagentur.de, `scout.py` can watch individual companies'
career pages directly — but only via their **applicant-tracking system's
official public feed**, never by scraping arbitrary HTML or hitting
LinkedIn. LinkedIn has no public API for this and is aggressively
anti-scraping (ToS-hostile, rate-limited, legal history of going after
scrapers) — not worth building against. A hand-rolled scraper for one
company's own custom-built careers page is *possible* (BeautifulSoup/
Playwright, one adapter per site) but fragile — it breaks on every redesign
and doesn't fit the stable `JobSource` interface below without a bespoke
parser per company.

What does fit cleanly: many companies' career pages are actually powered by
one of a handful of ATS platforms, which publish a public, no-auth,
official JSON/XML feed meant for exactly this kind of external read.
`ats_sources.py` implements three:

| source (`"source"` field) | platform | required params | check if a company uses it |
| --- | --- | --- | --- |
| `greenhouse` | Greenhouse | `board_token`, optional `employer`, `keywords`, `location` | careers page URL contains `greenhouse.io` or `boards.greenhouse.io/{board_token}` |
| `lever` | Lever | `site`, optional `employer`, `keywords`, `location`, `team`, `commitment` | careers page URL contains `jobs.lever.co/{site}` |
| `personio` | Personio | `company`, optional `employer`, `keywords`, `location`, `language` | careers page URL looks like `{company}.jobs.personio.de` — very common for German SMEs/Mittelstand |

`keywords` filters client-side (all words must appear, whole-word match) —
none of these three APIs support full-text search server-side, so `scout.py`
fetches every open posting for that board/site/company and filters locally,
the same shape as arbeitsagentur's `was` just resolved on your end instead
of theirs.

Example `queries.json` entries:

```json
{
  "label": "Musterfirma AG — Werkstudent (Greenhouse)",
  "source": "greenhouse",
  "params": { "board_token": "musterfirma", "employer": "Musterfirma AG", "keywords": "Werkstudent" }
},
{
  "label": "Musterfirma GmbH — Werkstudent Berlin (Lever)",
  "source": "lever",
  "params": { "site": "musterfirma", "employer": "Musterfirma GmbH", "keywords": "Werkstudent", "location": "Berlin" }
},
{
  "label": "Musterfirma KG — Werkstudent (Personio)",
  "source": "personio",
  "params": { "company": "musterfirma", "employer": "Musterfirma KG", "keywords": "Werkstudent" }
}
```

(Not included in `queries.example.json` itself, since these placeholder
tokens don't correspond to real companies and would just log search errors
every run — swap in real `board_token`/`site`/`company` values for
companies you're actually targeting.)

### Adding another source type

`scout.py`'s pipeline is written against the `JobSource` interface
(`job_source.py`), not against any one implementation directly. To add a
fourth (SmartRecruiters, Workable, a hand-rolled scraper for one specific
company, ...):

1. Write a class implementing `JobSource.search(**params)` and
   `JobSource.fetch_details(refnr)` (both must return normalised `Job`
   objects / plain text — never raise on a single failed request, since
   `fetch_details` failures are expected to degrade to "skip scoring", not
   crash the run).
2. Register it in `scout.py`'s `SOURCE_REGISTRY` under a short name.
3. Add `"source": "your-name"` to the relevant `queries.json` entries.

`scout.py` only spins up the sources actually referenced by your
`queries.json` (via a `contextlib.ExitStack`), so adding a source to the
registry doesn't require credentials for it until you actually add queries
for it.

## Auto-generate tailored CVs

Jobs scoring at or above `CV_SCORE_THRESHOLD` (default 7/10) get a separate
Telegram message with a "📄 CV generieren" button. Tapping it:

1. Is caught by `telegram_bot.py` — a small **always-on** process, separate
   from `scout.py`'s one-shot cron runs, long-polling Telegram for button
   clicks (`docker compose up -d bot`, or `python telegram_bot.py` locally).
2. Looks the job up in the shared SQLite db (already has the full
   Stellenbeschreibung from when `scout.py` scored it).
3. Extracts the text from your base CV (`cv.pdf` / `BASE_CV_PATH`) and asks
   the LLM to re-emphasise and reorder it for that specific job — it's
   instructed not to invent skills or experience that aren't in the original.
4. Renders the result as a new PDF and sends it back via Telegram.

**Important caveat:** the generated PDF is rendered from scratch with a
plain, simple layout — it is **not** a pixel-perfect copy of your original
CV's design/fonts/spacing. If your base CV has a carefully crafted layout,
treat the output as tailored *content* you copy into your own template, not
a drop-in replacement file.

This feature needs `GROQ_API_KEY` and `BASE_CV_PATH` set; without them
`telegram_bot.py` refuses to start, and `scout.py` simply skips sending the
CV-prompt buttons.

## Get your Telegram chat ID

1. Create a bot: message `@BotFather`, `/newbot`, save the token.
2. Open a chat with your bot, send any message (e.g. `start`).
3. Visit:
   ```
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```
4. Find `"chat":{"id":123456789,...}`. That's your `TELEGRAM_CHAT_ID`.

## Sample output

```
🎯 arbeitsagentur-scout
3 relevante neue Jobs (von 11 insgesamt)
─────────────────────

[9/10] Werkstudent KI & Automatisierung
🏢 Messe Düsseldorf GmbH · 📍 40474 Düsseldorf · 🗓 2026-04-28
Perfekter Fit: n8n + ChatGPT/Claude explizit gefordert, Standort identisch.
Python, n8n, ChatGPT, Claude, APIs
→ Inserat öffnen

[8/10] Junior Inhouse Consultant Digitalisierung
🏢 ARAG SE · 📍 40472 Düsseldorf · 🗓 2026-04-27
Stark: BWL-Schwerpunkt + Digitalisierungsprojekte, Bachelor reicht.
Power Platform, BWL, Prozessoptimierung
→ Inserat öffnen
```

## Project layout

```
arbeitsagentur-scout/
├── scout.py            # Entry point — orchestrates a single run (cron)
├── telegram_bot.py     # Always-on listener for the CV-generation button
├── arbeitsagentur.py   # API client (search + jobdetails), implements JobSource
├── job_source.py       # JobSource interface — the seam for multi-portal support
├── ats_sources.py       # Greenhouse / Lever / Personio JobSource implementations
├── analyzer.py         # Groq / Llama scoring against candidate profile
├── cv_generator.py     # PDF text extraction → LLM tailoring → PDF render
├── llm_utils.py         # Shared Groq JSON-retry + rate-limit backoff
├── notifier.py         # Telegram bot output (messages, buttons, documents)
├── storage.py          # SQLite dedup + history + bot poll offset
├── dashboard.py         # Optional read-only Streamlit UI over jobs.db
├── notion_sync.py       # Optional: mirror new jobs into a Notion database
├── tests/              # pytest suite (mocked APIs, no network needed)
│   └── test_integration.py  # end-to-end scout.main() run, all HTTP mocked
├── .github/workflows/  # CI — runs the test suite on every push
├── requirements.txt
├── requirements-dev.txt
├── requirements-dashboard.txt  # only needed for `streamlit run dashboard.py`
├── Dockerfile
├── docker-compose.yml  # scout (one-shot) + bot (long-running, healthchecked)
├── .env.example
├── profile.example.md  # copy to profile.md (gitignored) — your real background
├── queries.example.json # copy to queries.json (gitignored) — your real searches
├── cv.pdf               # your base CV (gitignored) — see BASE_CV_PATH
└── README.md
```

## Roadmap

- [x] arbeitsagentur API integration with full Stellenbeschreibung
- [x] LLM scoring with structured JSON output
- [x] Telegram notifications with chunked messages
- [x] SQLite deduplication + history
- [x] Docker + cron deployment
- [x] Test suite + CI
- [x] Auto-generate tailored CVs for high-scoring jobs (Telegram button)
- [x] Streamlit UI to browse historical scores
- [x] Sync job history to a Notion database (see [Sync to Notion](#sync-to-notion))
- [x] Retry/backoff on Groq rate limits and malformed JSON
- [x] `JobSource` abstraction for multi-portal support
- [x] Watch specific companies' career pages via Greenhouse/Lever/Personio's
      public feeds (see [Watching specific companies](#watching-specific-companies))
- [x] Heartbeat / dead-man's switch — a periodic "scout läuft" ping on an
      otherwise-silent run (`HEARTBEAT_HOURS`, default 24; 0 disables) so no
      messages means "no new jobs", not "cron/container/token is dead"
- [ ] Auto-draft Anschreiben for top-scoring jobs
- [ ] SmartRecruiters/Workable `JobSource` implementations
- [ ] LinkedIn / StepStone / Indeed — intentionally not planned; see
      [Watching specific companies](#watching-specific-companies) for why

## Notes

- The arbeitsagentur API is **not officially supported** — endpoints are
  reverse-engineered and documented by the [bundesAPI](https://github.com/bundesAPI/jobsuche-api)
  community. Don't abuse it (no concurrent scraping, low frequency, hobby use).
- The Groq free tier is generous but rate-limited. With ~5 queries × ~10 new
  jobs every 4 hours you stay well under the daily quota.
- This is a **personal tool**. Take inspiration, fork, but don't run a SaaS
  on top of an undocumented government API.

## License

MIT — see [LICENSE](LICENSE).
