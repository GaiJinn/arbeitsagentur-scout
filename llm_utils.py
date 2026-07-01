"""
llm_utils — shared helpers for calling Groq chat completions and getting back
parsed JSON, with two kinds of resilience the model/API needs on their own:

1. Rate limits (429): Groq's free tier throttles fairly aggressively. Back off
   and retry a few times (honoring `Retry-After` when the API sends one)
   instead of failing the whole run over a transient 429.
2. Malformed JSON: even with `response_format={"type": "json_object"}`, Llama
   occasionally returns truncated or invalid JSON. Ask it to fix its own
   output once or twice before giving up.

Used by both analyzer.py (job scoring) and cv_generator.py (CV tailoring) so
the retry/backoff logic only lives in one place.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import groq

log = logging.getLogger("llm_utils")

MAX_RATE_LIMIT_RETRIES = 3
RATE_LIMIT_BACKOFF_SECONDS = 2.0

MAX_JSON_RETRIES = 2  # extra attempts if the model's reply isn't valid JSON

_RETRY_JSON_INSTRUCTION = (
    "Das war kein gültiges JSON. Antworte erneut, AUSSCHLIESSLICH als "
    "gültiges JSON, ohne Markdown, ohne Codeblock, ohne Erklärtext."
)


def _retry_after_seconds(exc: groq.RateLimitError) -> float | None:
    response = getattr(exc, "response", None)
    header = response.headers.get("retry-after") if response is not None else None
    if not header:
        return None
    try:
        return float(header)
    except ValueError:
        return None


def _create_with_rate_limit_retry(
    client: groq.Groq,
    *,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
):
    attempt = 0
    while True:
        try:
            return client.chat.completions.create(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=temperature,
            )
        except groq.RateLimitError as exc:
            attempt += 1
            if attempt > MAX_RATE_LIMIT_RETRIES:
                log.error("Groq rate limit: giving up after %d attempts.", attempt)
                raise
            wait = _retry_after_seconds(exc)
            if wait is None:
                wait = RATE_LIMIT_BACKOFF_SECONDS * (2 ** (attempt - 1))
            log.warning(
                "Groq rate limited (attempt %d/%d) — waiting %.1fs before retry.",
                attempt, MAX_RATE_LIMIT_RETRIES, wait,
            )
            time.sleep(wait)


def call_llm_json(
    client: groq.Groq,
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.2,
) -> dict[str, Any]:
    """Call Groq chat.completions and return the parsed JSON body.

    Retries transient 429s with backoff, and re-prompts the model up to
    `MAX_JSON_RETRIES` times if its reply isn't valid JSON. Raises
    `json.JSONDecodeError` if the model still won't produce valid JSON after
    all retries, or the underlying `groq` exception if rate limits persist.
    """
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    response = _create_with_rate_limit_retry(
        client, model=model, messages=messages, temperature=temperature,
    )
    content = response.choices[0].message.content or "{}"

    for json_attempt in range(MAX_JSON_RETRIES + 1):
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            if json_attempt >= MAX_JSON_RETRIES:
                log.warning(
                    "Groq returned non-JSON after %d attempts, giving up: %s",
                    json_attempt + 1, content[:200],
                )
                raise
            log.warning(
                "Groq returned non-JSON (attempt %d/%d) — asking it to fix the format.",
                json_attempt + 1, MAX_JSON_RETRIES,
            )
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": _RETRY_JSON_INSTRUCTION})
            response = _create_with_rate_limit_retry(
                client, model=model, messages=messages, temperature=temperature,
            )
            content = response.choices[0].message.content or "{}"

    # Unreachable, but keeps type-checkers happy.
    raise json.JSONDecodeError("unreachable", "", 0)
