"""
cv_generator — tailor a base CV (PDF) to a specific job using an LLM, and
render the result back to a new PDF.

This does NOT reproduce the original PDF's layout/design. It extracts the
text, has the LLM re-emphasise/reorder it for the target job, and renders
a clean, simply-styled PDF from scratch.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from pathlib import Path

from groq import Groq
from pypdf import PdfReader

from llm_utils import call_llm_json
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    ListFlowable,
    ListItem,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)

# A single restrained accent color, used sparingly (section headings + two
# hairlines) so the output reads as "clean CV", not "LLM slideshow".
ACCENT_COLOR = HexColor("#1F4E5F")
MUTED_COLOR = HexColor("#666666")
RULE_COLOR = HexColor("#C9D2D6")

log = logging.getLogger("cv_generator")

TAILOR_MODEL = "llama-3.3-70b-versatile"

SYSTEM_PROMPT = (
    "Du bist ein Karriere-Coach, der einen bestehenden Lebenslauf für eine "
    "konkrete Stellenanzeige anpasst. Du darfst vorhandene Inhalte umformulieren, "
    "neu gewichten und die Reihenfolge ändern, um die Passung zur Stelle zu "
    "betonen. Erfinde NIEMALS neue Fähigkeiten, Erfahrungen, Titel oder "
    "Qualifikationen, die nicht im Original-Lebenslauf vorkommen. Antworte "
    "AUSSCHLIESSLICH als gültiges JSON, kein Markdown, kein Codeblock."
)

USER_TEMPLATE = """ORIGINAL-LEBENSLAUF (Rohtext, extrahiert aus PDF):
{cv_text}

ZIEL-STELLE:
Titel: {title}
Firma: {employer}
Ort: {location}
Beschreibung:
{description}

Aufgabe: Passe den Lebenslauf inhaltlich auf diese Stelle an (Reihenfolge,
Formulierung, Hervorhebungen) — ohne neue Fakten zu erfinden. Antworte als
JSON in genau diesem Schema:
{{
  "name": "<Name aus dem Original-Lebenslauf>",
  "headline": "<kurzer, auf die Stelle zugeschnittener Titel, max 10 Wörter>",
  "summary": "<2-4 Sätze Profil-Zusammenfassung, auf die Stelle zugeschnitten>",
  "contact": "<Kontaktzeile: Ort, E-Mail, Telefon — was im Original vorhanden ist>",
  "sections": [
    {{
      "title": "<z.B. Berufserfahrung>",
      "items": [
        {{"heading": "<z.B. Werkstudent bei X, 2024-2025>", "bullets": ["<Punkt>", "..."]}}
      ]
    }}
  ]
}}
"""


@dataclass
class TailoredCV:
    name: str
    headline: str
    summary: str
    contact: str
    sections: list[dict] = field(default_factory=list)


def extract_text_from_pdf(pdf_path: Path) -> str:
    reader = PdfReader(str(pdf_path))
    pages = [page.extract_text() or "" for page in reader.pages]
    text = "\n\n".join(p.strip() for p in pages if p.strip())
    if not text:
        raise ValueError(f"No extractable text in {pdf_path} (scanned/image-only PDF?).")
    return text


def tailor_cv(
    *,
    cv_text: str,
    job_title: str,
    job_employer: str,
    job_location: str,
    job_description: str,
    api_key: str,
    model: str = TAILOR_MODEL,
) -> TailoredCV:
    client = Groq(api_key=api_key)
    prompt = USER_TEMPLATE.format(
        cv_text=cv_text.strip()[:8000],
        title=job_title,
        employer=job_employer,
        location=job_location,
        description=(job_description or "").strip()[:4000],
    )
    # call_llm_json retries once/twice on malformed JSON and backs off on 429s;
    # a json.JSONDecodeError here means the model gave up entirely, and is
    # left to the caller (telegram_bot.py already reports failures to the user).
    data = call_llm_json(
        client,
        model=model,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=prompt,
        temperature=0.3,
    )
    return TailoredCV(
        name=data.get("name", ""),
        headline=data.get("headline", ""),
        summary=data.get("summary", ""),
        contact=data.get("contact", ""),
        sections=list(data.get("sections", [])),
    )


def render_cv_pdf(cv: TailoredCV) -> bytes:
    """Render a TailoredCV to PDF bytes.

    Deliberately simple, single-column layout (see module docstring — this is
    not a reproduction of the candidate's original design). The styling below
    is intentionally restrained: one accent color used only for the name
    hairline and section headings, generous whitespace, no boxes/backgrounds
    — the goal is "reads like a competently-typeset CV", not "look at all the
    ReportLab features".
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=18 * mm, bottomMargin=16 * mm,
    )
    styles = getSampleStyleSheet()
    name_style = ParagraphStyle(
        "CVName", parent=styles["Title"], fontSize=21, leading=24,
        alignment=0, spaceAfter=1,
    )
    headline_style = ParagraphStyle(
        "CVHeadline", parent=styles["Normal"], fontSize=12, leading=15,
        textColor=ACCENT_COLOR, fontName="Helvetica-Oblique", spaceAfter=3,
    )
    contact_style = ParagraphStyle(
        "CVContact", parent=styles["Normal"], fontSize=9, leading=12,
        textColor=MUTED_COLOR, spaceAfter=8,
    )
    body_style = ParagraphStyle(
        "CVBody", parent=styles["BodyText"], fontSize=10, leading=14, spaceAfter=6,
    )
    section_title_style = ParagraphStyle(
        "CVSection", parent=styles["Heading2"], fontSize=12.5, leading=15,
        textColor=ACCENT_COLOR, spaceBefore=14, spaceAfter=2,
    )
    item_heading_style = ParagraphStyle(
        "CVItemHeading", parent=styles["Normal"], fontSize=10.5, leading=14,
        spaceBefore=6, fontName="Helvetica-Bold",
    )
    bullet_style = ParagraphStyle(
        "CVBullet", parent=styles["Normal"], fontSize=10, leading=13.5,
    )

    story = []
    if cv.name:
        story.append(Paragraph(cv.name, name_style))
    if cv.headline:
        story.append(Paragraph(cv.headline, headline_style))
    if cv.contact:
        story.append(Paragraph(cv.contact, contact_style))
    # Hairline under the header block, only drawn if there's a header at all —
    # an all-empty CV (see tests) shouldn't render a floating rule.
    if cv.name or cv.headline or cv.contact:
        story.append(HRFlowable(width="100%", thickness=1, color=ACCENT_COLOR, spaceAfter=8))
    if cv.summary:
        story.append(Paragraph(cv.summary, body_style))

    for section in cv.sections:
        title = section.get("title", "")
        if title:
            story.append(Paragraph(title, section_title_style))
            story.append(HRFlowable(width="100%", thickness=0.6, color=RULE_COLOR, spaceAfter=4))
        for item in section.get("items", []):
            heading = item.get("heading", "")
            if heading:
                story.append(Paragraph(heading, item_heading_style))
            bullets = item.get("bullets", [])
            if bullets:
                story.append(
                    ListFlowable(
                        [ListItem(Paragraph(b, bullet_style), spaceAfter=2) for b in bullets],
                        bulletType="bullet",
                        leftIndent=12,
                        bulletColor=ACCENT_COLOR,
                    )
                )

    doc.build(story)
    return buf.getvalue()


def generate_tailored_cv_pdf(
    *,
    base_cv_path: Path,
    job_title: str,
    job_employer: str,
    job_location: str,
    job_description: str,
    api_key: str,
) -> bytes:
    """End-to-end: PDF in, tailored PDF bytes out."""
    cv_text = extract_text_from_pdf(base_cv_path)
    tailored = tailor_cv(
        cv_text=cv_text,
        job_title=job_title,
        job_employer=job_employer,
        job_location=job_location,
        job_description=job_description,
        api_key=api_key,
    )
    return render_cv_pdf(tailored)
