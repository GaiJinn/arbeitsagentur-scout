import json
from types import SimpleNamespace

import pytest

import cv_generator
from cv_generator import (
    TailoredCV,
    extract_text_from_pdf,
    generate_tailored_cv_pdf,
    render_cv_pdf,
    tailor_cv,
)


def make_tailored_cv() -> TailoredCV:
    return TailoredCV(
        name="Max Mustermann",
        headline="Werkstudent Software Engineering",
        summary="Motivierter Informatik-Student.",
        contact="Musterstadt · max@example.com",
        sections=[
            {
                "title": "Berufserfahrung",
                "items": [
                    {"heading": "Werkstudent bei Beispiel GmbH", "bullets": ["API-Integration", "Linux"]}
                ],
            },
            {"title": "Skills", "items": [{"heading": "", "bullets": ["Python", "Docker"]}]},
        ],
    )


def fake_groq_response(content: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def make_fake_groq_client(payload: dict):
    return SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **_: fake_groq_response(json.dumps(payload))
            )
        )
    )


# -- render_cv_pdf / extract_text_from_pdf -----------------------------------

def test_render_cv_pdf_produces_valid_pdf_bytes():
    pdf_bytes = render_cv_pdf(make_tailored_cv())
    assert pdf_bytes[:4] == b"%PDF"
    assert len(pdf_bytes) > 500


def test_render_cv_pdf_roundtrips_through_extraction(tmp_path):
    pdf_bytes = render_cv_pdf(make_tailored_cv())
    pdf_path = tmp_path / "cv.pdf"
    pdf_path.write_bytes(pdf_bytes)

    text = extract_text_from_pdf(pdf_path)
    assert "Max Mustermann" in text
    assert "Python" in text
    assert "Werkstudent bei Beispiel GmbH" in text


def test_render_cv_pdf_handles_missing_optional_fields():
    cv = TailoredCV(name="Max", headline="", summary="", contact="", sections=[])
    pdf_bytes = render_cv_pdf(cv)
    assert pdf_bytes[:4] == b"%PDF"


def test_extract_text_from_pdf_raises_on_empty_pdf(tmp_path):
    # A render with literally no content produces an unextractable/blank PDF.
    cv = TailoredCV(name="", headline="", summary="", contact="", sections=[])
    pdf_bytes = render_cv_pdf(cv)
    pdf_path = tmp_path / "blank.pdf"
    pdf_path.write_bytes(pdf_bytes)

    with pytest.raises(ValueError):
        extract_text_from_pdf(pdf_path)


# -- tailor_cv ----------------------------------------------------------------

def test_tailor_cv_parses_llm_response(monkeypatch):
    payload = {
        "name": "Max Mustermann",
        "headline": "Werkstudent KI",
        "summary": "Guter Fit für die Stelle.",
        "contact": "Musterstadt",
        "sections": [{"title": "Skills", "items": [{"heading": "", "bullets": ["Python"]}]}],
    }
    monkeypatch.setattr(cv_generator, "Groq", lambda api_key: make_fake_groq_client(payload))

    result = tailor_cv(
        cv_text="Max Mustermann, Informatik-Student.",
        job_title="Werkstudent KI",
        job_employer="Beispiel GmbH",
        job_location="Musterstadt",
        job_description="Suchen Werkstudent für KI-Projekte.",
        api_key="fake-key",
    )
    assert result.name == "Max Mustermann"
    assert result.headline == "Werkstudent KI"
    assert result.sections[0]["title"] == "Skills"


# -- generate_tailored_cv_pdf (end-to-end, LLM mocked) ------------------------

def test_generate_tailored_cv_pdf_end_to_end(tmp_path, monkeypatch):
    base_cv_path = tmp_path / "cv.pdf"
    base_cv_path.write_bytes(render_cv_pdf(make_tailored_cv()))

    payload = {
        "name": "Max Mustermann",
        "headline": "Werkstudent Backend",
        "summary": "Passt gut zur Stelle.",
        "contact": "Musterstadt",
        "sections": [{"title": "Skills", "items": [{"heading": "", "bullets": ["Python", "Docker"]}]}],
    }
    monkeypatch.setattr(cv_generator, "Groq", lambda api_key: make_fake_groq_client(payload))

    pdf_bytes = generate_tailored_cv_pdf(
        base_cv_path=base_cv_path,
        job_title="Werkstudent Backend",
        job_employer="Zielfirma",
        job_location="Berlin",
        job_description="Backend-Entwicklung mit Python.",
        api_key="fake-key",
    )
    assert pdf_bytes[:4] == b"%PDF"
