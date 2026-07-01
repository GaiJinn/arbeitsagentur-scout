"""
Tests for dashboard.py's data layer (load_jobs / filter_jobs / _parse_json_list)
— the pure, non-Streamlit parts. _render() itself needs a Streamlit script
run and isn't covered here.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

import dashboard
from storage import SCHEMA


def make_db(tmp_path):
    db_path = tmp_path / "jobs.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.execute(
        """INSERT INTO jobs
           (refnr, title, employer, location, posted_date, profession, url,
            description, score, summary, key_skills, fit_reasons, flags, seen_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("ref-1", "Werkstudent KI", "Firma A", "Düsseldorf", "2026-06-01", "Informatiker",
         "https://example.com/1", "desc", 9, "Guter Fit",
         json.dumps(["Python", "n8n"]), json.dumps(["Skill-Match"]), json.dumps([]),
         "2026-06-01T10:00:00+00:00"),
    )
    conn.execute(
        """INSERT INTO jobs
           (refnr, title, employer, location, posted_date, profession, url,
            description, score, summary, key_skills, fit_reasons, flags, seen_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("ref-2", "Junior Consultant", "Firma B", "Berlin", "2026-06-02", "Berater",
         "https://example.com/2", "desc", 4, "Mittelmäßiger Fit",
         json.dumps(["Excel"]), json.dumps([]), json.dumps(["low_salary"]),
         "2026-06-02T10:00:00+00:00"),
    )
    conn.execute(
        """INSERT INTO jobs
           (refnr, title, employer, location, posted_date, profession, url,
            description, score, summary, key_skills, fit_reasons, flags, seen_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("ref-3", "Unscored Job", "Firma C", "Köln", "2026-06-03", "",
         "https://example.com/3", "desc", None, None, None, None, None,
         "2026-06-03T10:00:00+00:00"),
    )
    conn.commit()
    conn.close()
    return db_path


# -- load_jobs ------------------------------------------------------------------

def test_load_jobs_returns_empty_df_for_missing_db(tmp_path):
    df = dashboard.load_jobs(tmp_path / "does-not-exist.db")
    assert df.empty
    assert "score" in df.columns


def test_load_jobs_parses_json_columns_and_numeric_score(tmp_path):
    df = dashboard.load_jobs(make_db(tmp_path))
    assert len(df) == 3
    row = df[df["refnr"] == "ref-1"].iloc[0]
    assert row["key_skills"] == ["Python", "n8n"]
    assert row["fit_reasons"] == ["Skill-Match"]
    assert row["flags"] == []
    assert row["score"] == 9

    unscored = df[df["refnr"] == "ref-3"].iloc[0]
    assert unscored["key_skills"] == []
    import pandas as pd
    assert pd.isna(unscored["score"])


def test_parse_json_list_handles_bad_input():
    assert dashboard._parse_json_list(None) == []
    assert dashboard._parse_json_list("") == []
    assert dashboard._parse_json_list("not json") == []
    assert dashboard._parse_json_list(json.dumps({"not": "a list"})) == []
    assert dashboard._parse_json_list(json.dumps(["a", "b"])) == ["a", "b"]


# -- filter_jobs ------------------------------------------------------------------

def test_filter_jobs_empty_df_returns_empty(tmp_path):
    df = dashboard.load_jobs(tmp_path / "missing.db")
    assert dashboard.filter_jobs(df).empty


def test_filter_jobs_min_score_excludes_low_scores_but_keeps_unscored(tmp_path):
    df = dashboard.load_jobs(make_db(tmp_path))
    result = dashboard.filter_jobs(df, min_score=8, include_unscored=True)
    refnrs = set(result["refnr"])
    assert refnrs == {"ref-1", "ref-3"}  # ref-2 (score 4) excluded, ref-3 unscored kept


def test_filter_jobs_can_exclude_unscored(tmp_path):
    df = dashboard.load_jobs(make_db(tmp_path))
    result = dashboard.filter_jobs(df, min_score=0, include_unscored=False)
    assert "ref-3" not in set(result["refnr"])


def test_filter_jobs_search_matches_title_employer_or_summary(tmp_path):
    df = dashboard.load_jobs(make_db(tmp_path))
    result = dashboard.filter_jobs(df, search="consultant")
    assert set(result["refnr"]) == {"ref-2"}


def test_filter_jobs_location_filter(tmp_path):
    df = dashboard.load_jobs(make_db(tmp_path))
    result = dashboard.filter_jobs(df, location="berlin")
    assert set(result["refnr"]) == {"ref-2"}


def test_filter_jobs_sorts_by_score_descending_with_unscored_last(tmp_path):
    df = dashboard.load_jobs(make_db(tmp_path))
    result = dashboard.filter_jobs(df, include_unscored=True)
    assert list(result["refnr"]) == ["ref-1", "ref-2", "ref-3"]
