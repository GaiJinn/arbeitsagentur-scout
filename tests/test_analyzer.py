import json
from types import SimpleNamespace

import pytest

import analyzer
from arbeitsagentur import Job


def make_job() -> Job:
    return Job(
        refnr="ref-1",
        title="Werkstudent KI",
        employer="Beispiel GmbH",
        location="Düsseldorf",
        posted_date="2026-06-01",
        profession="Informatiker",
        url="https://example.com/job",
        description="Suchen Werkstudent für KI-Projekte.",
    )


def fake_groq_response(content: str):
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice])


def test_load_candidate_profile_missing_file_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(analyzer, "PROFILE_PATH", tmp_path / "missing.md")
    with pytest.raises(FileNotFoundError):
        analyzer._load_candidate_profile()


def test_load_candidate_profile_reads_and_strips(tmp_path, monkeypatch):
    profile_file = tmp_path / "profile.md"
    profile_file.write_text("  Mein Profil  \n", encoding="utf-8")
    monkeypatch.setattr(analyzer, "PROFILE_PATH", profile_file)
    assert analyzer._load_candidate_profile() == "Mein Profil"


def test_score_parses_valid_json(tmp_path, monkeypatch):
    profile_file = tmp_path / "profile.md"
    profile_file.write_text("Test-Profil", encoding="utf-8")
    monkeypatch.setattr(analyzer, "PROFILE_PATH", profile_file)

    llm_analyzer = analyzer.LLMAnalyzer(api_key="fake-key")
    payload = {
        "score": 9,
        "summary": "Sehr guter Fit.",
        "key_skills": ["Python", "n8n"],
        "fit_reasons": ["Skill-Match"],
        "flags": [],
    }
    llm_analyzer.client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **_: fake_groq_response(json.dumps(payload))
            )
        )
    )

    result = llm_analyzer.score(make_job())
    assert result.score == 9
    assert result.summary == "Sehr guter Fit."
    assert result.key_skills == ["Python", "n8n"]


def test_score_handles_non_json_response(tmp_path, monkeypatch):
    profile_file = tmp_path / "profile.md"
    profile_file.write_text("Test-Profil", encoding="utf-8")
    monkeypatch.setattr(analyzer, "PROFILE_PATH", profile_file)

    llm_analyzer = analyzer.LLMAnalyzer(api_key="fake-key")
    llm_analyzer.client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **_: fake_groq_response("not json"))
        )
    )

    result = llm_analyzer.score(make_job())
    assert result.score == 0
    assert "parse_error" in result.flags


def test_score_truncates_skills_and_flags_lists(tmp_path, monkeypatch):
    profile_file = tmp_path / "profile.md"
    profile_file.write_text("Test-Profil", encoding="utf-8")
    monkeypatch.setattr(analyzer, "PROFILE_PATH", profile_file)

    llm_analyzer = analyzer.LLMAnalyzer(api_key="fake-key")
    payload = {
        "score": 5,
        "summary": "Ok",
        "key_skills": [f"skill-{i}" for i in range(10)],
        "fit_reasons": [f"reason-{i}" for i in range(10)],
        "flags": [f"flag-{i}" for i in range(10)],
    }
    llm_analyzer.client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **_: fake_groq_response(json.dumps(payload))
            )
        )
    )

    result = llm_analyzer.score(make_job())
    assert len(result.key_skills) == 6
    assert len(result.fit_reasons) == 4
    assert len(result.flags) == 4


def test_llm_analyzer_requires_api_key(tmp_path, monkeypatch):
    profile_file = tmp_path / "profile.md"
    profile_file.write_text("Test-Profil", encoding="utf-8")
    monkeypatch.setattr(analyzer, "PROFILE_PATH", profile_file)

    with pytest.raises(ValueError):
        analyzer.LLMAnalyzer(api_key="")
