# 🎯 arbeitsagentur-scout

> Personal job-hunting bot for the German federal employment agency
> ([arbeitsagentur.de](https://www.arbeitsagentur.de/)) — automated search,
> LLM-based ranking, deduplication, Telegram alerts.

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

It's a cron-friendly one-shot script — run it, it does its job, exits.

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
       └─────────────────────────┘    └────────────────────┘
                            │
                            ▼
                     ┌─────────────┐
                     │  SQLite     │
                     │  (dedup)    │
                     └─────────────┘
```

## Tech stack

- **Python 3.12** — `httpx` for HTTP, `groq` for LLM, stdlib `sqlite3`
- **Groq Cloud** with `llama-3.3-70b-versatile` (free tier covers daily use)
- **Telegram Bot API** for notifications
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

### 4. Deploy on a VPS, cron every 4 hours

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
├── scout.py            # Entry point — orchestrates a single run
├── arbeitsagentur.py   # API client (search + jobdetails)
├── analyzer.py         # Groq / Llama scoring against candidate profile
├── notifier.py         # Telegram bot output
├── storage.py          # SQLite dedup + history
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── profile.example.md  # copy to profile.md (gitignored) — your real background
├── queries.example.json # copy to queries.json (gitignored) — your real searches
└── README.md
```

## Roadmap

- [x] arbeitsagentur API integration with full Stellenbeschreibung
- [x] LLM scoring with structured JSON output
- [x] Telegram notifications with chunked messages
- [x] SQLite deduplication + history
- [x] Docker + cron deployment
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
