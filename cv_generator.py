"""
cv_generator — tailor a base CV (PDF) to a specific job using an LLM, and
render the result back to a new PDF.

This does NOT reproduce the original PDF's layout/design. It extracts the
text, has the LLM re-emphasise/reorder it for the target job, and renders
a clean, simply-styled PDF from scratch.
"""
from __future__ import annotations

import io
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from groq import Groq
from pypdf import PdfReader
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    ListFlowable,
    ListItem,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)

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
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.3,
    )
    content = response.choices[0].message.content or "{}"
    data = json.loads(content)
    return TailoredCV(
        name=data.get("name", ""),
        headline=data.get("headline", ""),
        summary=data.get("summary", ""),
        contact=data.get("contact", ""),
        sections=list(data.get("sections", [])),
    )


def render_cv_pdf(cv: TailoredCV) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
    )
    styles = getSampleStyleSheet()
    name_style = ParagraphStyle("CVName", parent=styles["Title"], fontSize=20, spaceAfter=2)
    headline_style = ParagraphStyle("CVHeadline", parent=styles["Normal"], fontSize=12, textColor="#444444", spaceAfter=4)
    contact_style = ParagraphStyle("CVContact", parent=styles["Normal"], fontSize=9, textColor="#666666", spaceAfter=10)
    section_title_style = ParagraphStyle("CVSection", parent=styles["Heading2"], spaceBefore=12, spaceAfter=4)
    item_heading_style = ParagraphStyle("CVItemHeading", parent=styles["Normal"], fontSize=10.5, leading=14, spaceBefore=4, fontName="Helvetica-Bold")
    bullet_style = ParagraphStyle("CVBullet", parent=styles["Normal"], fontSize=10, leading=13)

    story = []
    if cv.name:
        story.append(Paragraph(cv.name, name_style))
    if cv.headline:
        story.append(Paragraph(cv.headline, headline_style))
    if cv.contact:
        story.append(Paragraph(cv.contact, contact_style))
    if cv.summary:
        story.append(Paragraph(cv.summary, styles["BodyText"]))
        story.append(Spacer(1, 6))

    for section in cv.sections:
        title = section.get("title", "")
        if title:
            story.append(Paragraph(title, section_title_style))
        for item in section.get("items", []):
            heading = item.get("heading", "")
            if heading:
                story.append(Paragraph(heading, item_heading_style))
            bullets = item.get("bullets", [])
            if bullets:
                story.append(
                    ListFlowable(
                        [ListItem(Paragraph(b, bullet_style)) for b in bullets],
                        bulletType="bullet",
                        leftIndent=12,
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
