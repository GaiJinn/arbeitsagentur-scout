"""One-off manual check: confirm NOTION_API_KEY / NOTION_PARENT_PAGE_ID in
.env are valid and the integration can create a database under your page.

NOT a pytest test — it loads the real .env and talks to the live Notion API
at import time. It used to be named test_notion_connection.py, which made a
bare `pytest` run collect it: that leaked the production Notion key into the
test process and called the real API during collection. Hence the rename
(plus `testpaths = tests` in pytest.ini).

Run locally:
    python notion_connection_check.py

Safe to run multiple times — the database id gets cached in jobs.db's
bot_state table after the first successful run, so a second run just
confirms the cached id still works instead of creating a duplicate database.
Delete jobs.db (or the notion_database_id row) if you want to force a fresh
database creation.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from notion_sync import NotionSync, NotionSyncError  # noqa: E402
from storage import JobStorage  # noqa: E402

api_key = os.getenv("NOTION_API_KEY")
parent_page_id = os.getenv("NOTION_PARENT_PAGE_ID")

if not api_key or not parent_page_id:
    print("NOTION_API_KEY and/or NOTION_PARENT_PAGE_ID missing from .env — nothing to test.")
    sys.exit(1)

storage = JobStorage(Path(__file__).parent / "jobs.db")

print(f"Using token …{api_key[-4:]}, parent page {parent_page_id}")

try:
    with NotionSync(api_key=api_key, parent_page_id=parent_page_id, storage=storage) as sync:
        db_id = sync.ensure_database()
    print(f"✅ Connected. 'Job Scout' database id: {db_id}")
    print("Check your Notion page — a database called 'Job Scout' should now be there.")
except NotionSyncError as exc:
    print(f"❌ Notion API error: {exc}")
    print("Common causes: token pasted wrong, or the integration hasn't been given")
    print("access to that page (Notion page → ••• → Connections → add your integration).")
    sys.exit(1)
