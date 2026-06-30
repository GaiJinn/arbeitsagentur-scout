"""
analyzer — LLM-based job ranking via Groq (Llama 3.3 70B).
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from groq import Groq

from arbeitsagentur import Job

log = logging.getLogger("analyzer")

# Candidate profile is loaded from a local file (gitignored) so personal
# details never live in source control. See profile.example.md for the
# format — copy it to profile.md and fill in your own background.
PROFILE_PATH = Path(os.getenv("PROFILE_PATH") or Path(__file__).parent / "profile.md")


def _load_candidate_profile() -> str:
    if not PROFILE_PATH.exists():
        raise FileNotFoundError(
            f"Candidate profile not found at {PROFILE_PATH}. "
            "Copy profile.example.md to profile.md and fill in your background, "
            "or set PROFILE_PATH to point elsewhere."
        )
    return PROFILE_PATH.read_text(encoding="utf-8").strip()


@dataclass
class JobScore:
    score: int  # 1-10
    summary: str  # one-sentence rationale
    key_skills: list[str] = field(default_factory=list)
    fit_reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


SYSTEM_PROMPT = (
    "Du bist ein Karriere-Berater, der Stellenanzeigen für einen konkreten "
    "Kandidaten bewertet. Antworte AUSSCHLIESSLICH als gültiges JSON, kein "
    "Markdown, kein Codeblock."
)

USER_TEMPLATE = """KANDIDATEN-PROFIL:
{profile}

STELLE:
Titel: {title}
Firma: {employer}
Ort: {location}
Beruf: {profession}
URL: {url}
Beschreibung:
{description}

Aufgabe: Bewerte diese Stelle für den Kandidaten von 1 (passt überhaupt nicht)
bis 10 (Idealfall). Antworte als JSON in genau diesem Schema:
{{
  "score": <int 1-10>,
  "summary": "<EIN Satz, max 30 Wörter — warum passt es / warum nicht>",
  "key_skills": ["skill", "skill", "skill"],
  "fit_reasons": ["kurzer Punkt", "kurzer Punkt"],
  "flags": ["Warnung 1", ...]
}}
"""


class LLMAnalyzer:
    def __init__(self, *, api_key: str, model: str = "llama-3.3-70b-versatile") -> None:
        if not api_key:
            raise ValueError("Groq API key is required.")
        self.client = Groq(api_key=api_key)
        self.model = model
        self.candidate_profile = _load_candidate_profile()

    def score(self, job: Job) -> JobScore:
        prompt = USER_TEMPLATE.format(
            profile=self.candidate_profile,
            title=job.title,
            employer=job.employer,
            location=job.location,
            profession=job.profession,
            url=job.url,
            description=(job.description or "").strip()[:4000],
        )
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
        )
        content = response.choices[0].message.content or "{}"
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            log.warning("LLM returned non-JSON: %s", content[:200])
            return JobScore(score=0, summary="LLM-Parse-Fehler",
                            key_skills=[], fit_reasons=[], flags=["parse_error"])

        return JobScore(
            score=int(data.get("score", 0)),
            summary=data.get("summary", ""),
            key_skills=list(data.get("key_skills", []))[:6],
            fit_reasons=list(data.get("fit_reasons", []))[:4],
            flags=list(data.get("flags", []))[:4],
        )
