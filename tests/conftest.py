"""Make the test suite hermetic, regardless of the machine it runs on.

scout.py and telegram_bot.py read their configuration from the environment
(and .env) at import time. Without the setup below, running pytest on a
machine with a populated production .env leaks real API keys into the test
process and flips config-dependent behaviour (e.g. Notion sync fires inside
tests that assume it's off) — so the suite would pass on a fresh CI checkout
but fail on the deployment box, or vice versa.

This module-level code runs before pytest imports any test module, i.e.
before the first `import scout`.
"""
from __future__ import annotations

import os
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent

# 1. Never load the real .env into the test process (see guard in scout.py).
os.environ["SCOUT_SKIP_DOTENV"] = "1"

# 2. Scrub config vars that may be exported in the developer's shell.
for _var in (
    "GROQ_API_KEY",
    "TELEGRAM_TOKEN",
    "TELEGRAM_CHAT_ID",
    "NOTION_API_KEY",
    "NOTION_PARENT_PAGE_ID",
    "DB_PATH",
    "SCORE_THRESHOLD",
    "CV_SCORE_THRESHOLD",
    "HEARTBEAT_HOURS",
    "MAX_LLM_CALLS_PER_RUN",
    "HEALTHCHECK_URL",
):
    os.environ.pop(_var, None)

# 3. Pin file-based config to committed, deterministic paths:
#    - queries.example.json is in git, so `import scout` also works on a fresh
#      CI checkout where the gitignored personal queries.json doesn't exist
#      (scout.py sys.exits at import when its queries file is missing).
#    - BASE_CV_PATH points at a file that never exists, so a cv.pdf lying in
#      the repo checkout can't switch on the CV-prompt path mid-test.
os.environ["QUERIES_PATH"] = str(_REPO / "queries.example.json")
os.environ["BASE_CV_PATH"] = str(_REPO / "tests" / "does-not-exist.pdf")
