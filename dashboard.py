"""
dashboard — read-only Streamlit UI to browse the job history scout.py builds
up in SQLite (jobs.db): filter by score/employer/location, see the LLM's
summary/flags/skills for each posting, and open the original listing.

This never writes to the db — it's purely a viewer, safe to run alongside a
cron'd scout.py or the always-on telegram_bot.py without any locking concerns
(SQLite handles concurrent readers fine; only writers can conflict).

Usage:
    pip install -r requirements-dashboard.txt
    streamlit run dashboard.py
    # or, to point at a specific db:
    DB_PATH=./data/jobs.db streamlit run dashboard.py
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pandas as pd

DEFAULT_DB_PATH = Path(__file__).parent / "data" / "jobs.db"
DB_PATH = Path(os.getenv("DB_PATH") or DEFAULT_DB_PATH)

DISPLAY_COLUMNS = [
    "score", "title", "employer", "location", "posted_date", "seen_at",
    "summary", "key_skills", "flags", "url",
]


def load_jobs(db_path: Path) -> pd.DataFrame:
    """Read every row of the jobs table into a DataFrame.

    Returns an empty (but correctly-columned) DataFrame if the db doesn't
    exist yet — lets the dashboard render a helpful empty state instead of
    crashing on a fresh checkout before scout.py has run once.
    """
    if not db_path.exists():
        return pd.DataFrame(columns=[
            "refnr", "title", "employer", "location", "posted_date",
            "profession", "url", "description", "score", "summary",
            "key_skills", "fit_reasons", "flags", "seen_at",
        ])
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        df = pd.read_sql_query("SELECT * FROM jobs", conn)

    for json_col in ("key_skills", "fit_reasons", "flags"):
        df[json_col] = df[json_col].apply(_parse_json_list)
    df["score"] = pd.to_numeric(df["score"], errors="coerce")
    return df


def _parse_json_list(raw: object) -> list[str]:
    if not raw or not isinstance(raw, str):
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def filter_jobs(
    df: pd.DataFrame,
    *,
    min_score: int = 0,
    include_unscored: bool = True,
    search: str = "",
    location: str = "",
) -> pd.DataFrame:
    """Apply the sidebar filters. Pure function — kept separate from the
    Streamlit rendering code below so it's testable without a Streamlit
    runtime."""
    if df.empty:
        return df

    if include_unscored:
        score_mask = df["score"].isna() | (df["score"] >= min_score)
    else:
        score_mask = df["score"] >= min_score
    result = df[score_mask]

    if search:
        needle = search.strip().lower()
        haystack = (
            result["title"].fillna("") + " " +
            result["employer"].fillna("") + " " +
            result["summary"].fillna("")
        ).str.lower()
        result = result[haystack.str.contains(needle, regex=False)]

    if location:
        result = result[result["location"].fillna("").str.contains(location, case=False, regex=False)]

    return result.sort_values("score", ascending=False, na_position="last")


def _render() -> None:
    """The actual Streamlit page. Only runs under `streamlit run`."""
    import streamlit as st

    st.set_page_config(page_title="arbeitsagentur-scout · Job History", page_icon="🎯", layout="wide")
    st.title("🎯 arbeitsagentur-scout — Job History")
    st.caption(f"Reading {DB_PATH} (read-only)")

    df = load_jobs(DB_PATH)
    if df.empty:
        st.info(
            "No jobs recorded yet. Run `python scout.py` at least once, or "
            "point `DB_PATH` at an existing jobs.db."
        )
        return

    with st.sidebar:
        st.header("Filters")
        min_score = st.slider("Minimum score", 0, 10, 0)
        include_unscored = st.checkbox("Include unscored jobs", value=True)
        search = st.text_input("Search title / employer / summary")
        location = st.text_input("Location contains")

    filtered = filter_jobs(
        df, min_score=min_score, include_unscored=include_unscored,
        search=search, location=location,
    )

    col1, col2, col3 = st.columns(3)
    col1.metric("Jobs shown", len(filtered))
    col1.metric("Total in db", len(df))
    scored = df["score"].dropna()
    col2.metric("Average score", f"{scored.mean():.1f}" if len(scored) else "—")
    col3.metric("Scored ≥ 8", int((scored >= 8).sum()))

    if len(scored):
        st.subheader("Score distribution")
        st.bar_chart(scored.value_counts().sort_index())

    st.subheader(f"Jobs ({len(filtered)})")
    for _, row in filtered.iterrows():
        score_label = f"[{int(row['score'])}/10]" if pd.notna(row["score"]) else "[--]"
        with st.expander(f"{score_label} {row['title']} — {row['employer']} ({row['location']})"):
            if row.get("summary"):
                st.write(row["summary"])
            if row.get("key_skills"):
                st.write("**Skills:** " + ", ".join(row["key_skills"]))
            if row.get("flags"):
                st.warning(", ".join(row["flags"]))
            st.caption(f"Posted {row.get('posted_date', '—')} · seen {row.get('seen_at', '—')}")
            if row.get("url"):
                st.markdown(f"[→ Open listing]({row['url']})")


if __name__ == "__main__":
    _render()
