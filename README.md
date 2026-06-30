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
- **Docker** for clean, reproducible deployment
- **Bundesagentur für Arbeit Jobsuche API** (community-documented at
  [bundesAPI/jobsuche-api](https://github.com/bundesAPI/jobsuche-api))

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
keys needed) and cover pagination, retry/backoff, JSON parsing, and SQLite
dedup. CI runs this on every push via [GitHub Actions](.github/workflows/ci.yml).

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
├── arbeitsagentur.py   # API client (search + jobdetails)
├── analyzer.py         # Groq / Llama scoring against candidate profile
├── cv_generator.py     # PDF text extraction → LLM tailoring → PDF render
├── notifier.py         # Telegram bot output (messages, buttons, documents)
├── storage.py          # SQLite dedup + history + bot poll offset
├── tests/              # pytest suite (mocked APIs, no network needed)
├── .github/workflows/  # CI — runs the test suite on every push
├── requirements.txt
├── requirements-dev.txt
├── Dockerfile
├── docker-compose.yml  # scout (one-shot) + bot (long-running) services
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
- [ ] Streamlit UI to browse historical scores
- [ ] Auto-draft Anschreiben for top-scoring jobs
- [ ] Multi-portal support (StepStone, Indeed, LinkedIn API)

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
