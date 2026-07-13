"""
Unit tests for scout.py's core pipeline (_collect_new_jobs), independent of
the integration test in test_integration.py which drives main() end-to-end
over mocked HTTP.
"""
from __future__ import annotations

import logging
import os
from types import SimpleNamespace

import httpx
import pytest
import respx

os.environ.setdefault("GROQ_API_KEY", "")
os.environ.setdefault("TELEGRAM_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_ID", "")

import scout  # noqa: E402
from analyzer import JobScore  # noqa: E402
from arbeitsagentur import Job  # noqa: E402


def make_job(refnr="job-1") -> Job:
    return Job(
        refnr=refnr,
        title="Werkstudent KI",
        employer="Beispiel GmbH",
        location="Düsseldorf",
        posted_date="2026-06-01",
        profession="Informatiker",
        description="Informatiker — Werkstudent KI",  # the thin from_api default
    )


class FakeClient:
    def __init__(self, jobs, details: dict[str, str]):
        self._jobs = jobs
        self._details = details

    def search(self, **_):
        return self._jobs

    def fetch_details(self, refnr):
        return self._details.get(refnr, "")


class FakeStorage:
    def __init__(self):
        self.seen: set[str] = set()
        self.saved: list[tuple] = []

    def has_job(self, refnr):
        return refnr in self.seen

    def save(self, job, score):
        self.seen.add(job.refnr)
        self.saved.append((job, score))


class FakeAnalyzer:
    """Records which jobs it was asked to score, always returns a fixed score."""

    def __init__(self, fixed_score=8):
        self.scored_refnrs: list[str] = []
        self.fixed_score = fixed_score

    def score(self, job) -> JobScore:
        self.scored_refnrs.append(job.refnr)
        return JobScore(score=self.fixed_score, summary="ok")


def _patch_queries(monkeypatch, queries):
    monkeypatch.setattr(scout, "SEARCH_QUERIES", queries)


def _sources(client) -> dict[str, object]:
    return {"arbeitsagentur": client}


def test_skips_already_seen_jobs(monkeypatch):
    _patch_queries(monkeypatch, [{"label": "q1", "params": {}}])
    storage = FakeStorage()
    storage.seen.add("job-1")
    client = FakeClient([make_job("job-1")], details={})
    analyzer = FakeAnalyzer()

    new_jobs, _ = scout._collect_new_jobs(_sources(client), storage, analyzer)

    assert new_jobs == []
    assert analyzer.scored_refnrs == []


def test_scores_job_when_details_available(monkeypatch):
    _patch_queries(monkeypatch, [{"label": "q1", "params": {}}])
    storage = FakeStorage()
    client = FakeClient(
        [make_job("job-1")],
        details={"job-1": "Vollständige Stellenbeschreibung mit viel Kontext."},
    )
    analyzer = FakeAnalyzer(fixed_score=9)

    new_jobs, _ = scout._collect_new_jobs(_sources(client), storage, analyzer)

    assert len(new_jobs) == 1
    job, score = new_jobs[0]
    assert analyzer.scored_refnrs == ["job-1"]
    assert job.description == "Vollständige Stellenbeschreibung mit viel Kontext."
    assert score.score == 9
    assert storage.saved[0][1].score == 9


def test_skips_scoring_when_details_fetch_returns_empty(monkeypatch):
    """A failed detail fetch must NOT fall back to scoring on the thin
    'beruf — titel' description — it should skip the LLM call entirely and
    save the job unscored so it can be reconsidered without a bad score."""
    _patch_queries(monkeypatch, [{"label": "q1", "params": {}}])
    storage = FakeStorage()
    client = FakeClient([make_job("job-1")], details={})  # no detail text
    analyzer = FakeAnalyzer()

    new_jobs, _ = scout._collect_new_jobs(_sources(client), storage, analyzer)

    assert len(new_jobs) == 1
    job, score = new_jobs[0]
    assert score is None
    assert analyzer.scored_refnrs == []  # never called
    assert job.description == "Informatiker — Werkstudent KI"  # untouched fallback
    assert storage.saved[0][1] is None


def test_skips_scoring_when_details_fetch_raises(monkeypatch):
    _patch_queries(monkeypatch, [{"label": "q1", "params": {}}])
    storage = FakeStorage()

    class RaisingClient(FakeClient):
        def fetch_details(self, refnr):
            raise RuntimeError("boom")

    client = RaisingClient([make_job("job-1")], details={})
    analyzer = FakeAnalyzer()

    new_jobs, _ = scout._collect_new_jobs(_sources(client), storage, analyzer)

    assert len(new_jobs) == 1
    _, score = new_jobs[0]
    assert score is None
    assert analyzer.scored_refnrs == []


def test_no_analyzer_saves_job_unscored(monkeypatch):
    _patch_queries(monkeypatch, [{"label": "q1", "params": {}}])
    storage = FakeStorage()
    client = FakeClient([make_job("job-1")], details={"job-1": "full text"})

    new_jobs, _ = scout._collect_new_jobs(_sources(client), storage, analyzer=None)

    assert len(new_jobs) == 1
    _, score = new_jobs[0]
    assert score is None


def test_known_job_reseen_with_region_becomes_region_sighting(monkeypatch):
    """A multi-location posting already claimed by an earlier query must not
    be re-saved, but its (refnr, region) sighting is reported so notion_sync
    can append the city to the existing page."""
    _patch_queries(monkeypatch, [{"label": "q-berlin", "region": "Berlin", "params": {}}])
    storage = FakeStorage()
    storage.seen.add("job-1")
    client = FakeClient([make_job("job-1")], details={})
    analyzer = FakeAnalyzer()

    new_jobs, sightings = scout._collect_new_jobs(_sources(client), storage, analyzer)

    assert new_jobs == []
    assert sightings == [("job-1", "Berlin")]
    assert analyzer.scored_refnrs == []


def test_known_job_reseen_without_region_reports_nothing(monkeypatch):
    _patch_queries(monkeypatch, [{"label": "q1", "params": {}}])  # no "region"
    storage = FakeStorage()
    storage.seen.add("job-1")
    client = FakeClient([make_job("job-1")], details={})

    new_jobs, sightings = scout._collect_new_jobs(_sources(client), storage, None)

    assert new_jobs == []
    assert sightings == []


def test_region_sightings_deduped_within_run(monkeypatch):
    _patch_queries(monkeypatch, [
        {"label": "q-berlin-a", "region": "Berlin", "params": {"was": "KI"}},
        {"label": "q-berlin-b", "region": "Berlin", "params": {"was": "LLM"}},
    ])
    storage = FakeStorage()
    storage.seen.add("job-1")
    client = FakeClient([make_job("job-1")], details={})

    _, sightings = scout._collect_new_jobs(_sources(client), storage, None)

    assert sightings == [("job-1", "Berlin")]  # once, not per query


def test_new_job_not_reported_as_region_sighting(monkeypatch):
    """A job first seen this run is saved + synced with its own region; only
    *pre-existing* jobs become sightings — but a second query re-seeing the
    just-saved job in another city does."""
    _patch_queries(monkeypatch, [
        {"label": "q-muenchen", "region": "München", "params": {"was": "KI"}},
        {"label": "q-berlin", "region": "Berlin", "params": {"was": "KI"}},
    ])
    storage = FakeStorage()
    client = FakeClient([make_job("job-1")], details={"job-1": "full text"})

    new_jobs, sightings = scout._collect_new_jobs(_sources(client), storage, None)

    assert [j.refnr for j, _ in new_jobs] == ["job-1"]
    assert new_jobs[0][0].extra["region"] == "München"  # first query wins the page
    assert sightings == [("job-1", "Berlin")]  # second query adds its city


def test_search_failure_for_one_query_does_not_abort_others(monkeypatch):
    class FlakyClient:
        def search(self, **params):
            if params.get("was") == "broken":
                raise RuntimeError("API down")
            return [make_job("job-2")]

        def fetch_details(self, refnr):
            return "full text"

    _patch_queries(monkeypatch, [
        {"label": "broken query", "params": {"was": "broken"}},
        {"label": "ok query", "params": {"was": "fine"}},
    ])
    storage = FakeStorage()
    analyzer = FakeAnalyzer()

    new_jobs, _ = scout._collect_new_jobs(_sources(FlakyClient()), storage, analyzer)

    assert len(new_jobs) == 1
    assert new_jobs[0][0].refnr == "job-2"


class FakeBotStorage(FakeStorage):
    """FakeStorage plus the bot_state key/value store the heartbeat needs."""

    def __init__(self):
        super().__init__()
        self.state: dict[str, str] = {}

    def get_bot_state(self, key, default=None):
        return self.state.get(key, default)

    def set_bot_state(self, key, value):
        self.state[key] = value


class RecordingNotifier:
    def __init__(self, ok=True):
        self.ok = ok
        self.texts: list[str] = []

    def send_text(self, text):
        self.texts.append(text)
        return self.ok


def test_heartbeat_no_extra_ping_when_alert_sent():
    storage = FakeBotStorage()
    notifier = RecordingNotifier()
    scout._handle_heartbeat(notifier, storage, alert_sent=True, quiet=False, total_new=3)
    assert notifier.texts == []  # the alert itself proves liveness
    assert scout.HEARTBEAT_STATE_KEY in storage.state  # window still reset


def test_heartbeat_pings_on_quiet_run_when_due(monkeypatch):
    monkeypatch.setattr(scout, "HEARTBEAT_HOURS", 24)
    storage = FakeBotStorage()  # no prior heartbeat → due immediately
    notifier = RecordingNotifier()
    scout._handle_heartbeat(notifier, storage, alert_sent=False, quiet=True, total_new=5)
    assert len(notifier.texts) == 1
    assert "läuft" in notifier.texts[0]
    assert storage.state.get(scout.HEARTBEAT_STATE_KEY)


def test_heartbeat_suppressed_when_recent(monkeypatch):
    monkeypatch.setattr(scout, "HEARTBEAT_HOURS", 24)
    storage = FakeBotStorage()
    storage.state[scout.HEARTBEAT_STATE_KEY] = scout._now_iso()  # pinged just now
    notifier = RecordingNotifier()
    scout._handle_heartbeat(notifier, storage, alert_sent=False, quiet=True, total_new=0)
    assert notifier.texts == []


def test_heartbeat_not_sent_when_send_failed_so_run_not_quiet(monkeypatch):
    # high_value existed but the alert send failed (quiet=False): don't paper
    # over that failure with a falsely-reassuring "all good" ping.
    monkeypatch.setattr(scout, "HEARTBEAT_HOURS", 24)
    storage = FakeBotStorage()
    notifier = RecordingNotifier()
    scout._handle_heartbeat(notifier, storage, alert_sent=False, quiet=False, total_new=2)
    assert notifier.texts == []


def test_heartbeat_disabled_when_hours_zero(monkeypatch):
    monkeypatch.setattr(scout, "HEARTBEAT_HOURS", 0)
    storage = FakeBotStorage()
    notifier = RecordingNotifier()
    scout._handle_heartbeat(notifier, storage, alert_sent=False, quiet=True, total_new=1)
    assert notifier.texts == []


def test_heartbeat_noop_without_notifier(monkeypatch):
    # By design: the heartbeat is a Telegram feature, so without a notifier
    # there is nothing to ping through — liveness monitoring for
    # Telegram-less setups is HEALTHCHECK_URL's job, not the heartbeat's.
    monkeypatch.setattr(scout, "HEARTBEAT_HOURS", 24)
    storage = FakeBotStorage()
    scout._handle_heartbeat(None, storage, alert_sent=False, quiet=True, total_new=0)
    assert storage.state == {}  # nothing sent, heartbeat window untouched


def test_no_description_skip_logged_at_info(monkeypatch, caplog):
    """Missing Stellenbeschreibung is an expected, common condition (agency
    reposts) — it must log at INFO, not WARNING, so quiet environments aren't
    flooded with per-job false alarms."""
    _patch_queries(monkeypatch, [{"label": "q1", "params": {}}])
    storage = FakeStorage()
    client = FakeClient([make_job("job-1")], details={})
    with caplog.at_level(logging.INFO, logger="scout"):
        scout._collect_new_jobs(_sources(client), storage, FakeAnalyzer())
    hits = [r for r in caplog.records if "No Stellenbeschreibung" in r.getMessage()]
    assert hits, "expected the scoring skip to be logged"
    assert {r.levelname for r in hits} == {"INFO"}


def test_channel_status_unconfigured_telegram_logs_info(monkeypatch, caplog):
    # Both credentials absent = intentional feature-off (tests, autodev,
    # console-only setups) — must not warn on every run.
    monkeypatch.setattr(scout, "TELEGRAM_TOKEN", "")
    monkeypatch.setattr(scout, "TELEGRAM_CHAT_ID", "")
    with caplog.at_level(logging.INFO, logger="scout"):
        scout._log_channel_status(analyzer=FakeAnalyzer(), notifier=None, notion=None)
    hits = [r for r in caplog.records if "Telegram" in r.getMessage()]
    assert [r.levelname for r in hits] == ["INFO"]


def test_channel_status_half_configured_telegram_warns(monkeypatch, caplog):
    # Exactly one of the pair set is almost certainly a config mistake and
    # must stay loud.
    monkeypatch.setattr(scout, "TELEGRAM_TOKEN", "123:abc")
    monkeypatch.setattr(scout, "TELEGRAM_CHAT_ID", "")
    with caplog.at_level(logging.INFO, logger="scout"):
        scout._log_channel_status(analyzer=FakeAnalyzer(), notifier=None, notion=None)
    hits = [r for r in caplog.records if "Telegram" in r.getMessage()]
    assert [r.levelname for r in hits] == ["WARNING"]


def test_llm_call_cap_defers_excess_jobs(monkeypatch):
    """With a cap of 2, only the first 2 jobs are scored; the 3rd is deferred
    (left unsaved) so a later run picks it up instead of alerting it unscored."""
    monkeypatch.setattr(scout, "MAX_LLM_CALLS_PER_RUN", 2)
    _patch_queries(monkeypatch, [{"label": "q1", "params": {}}])
    storage = FakeStorage()
    jobs = [make_job("j1"), make_job("j2"), make_job("j3")]
    client = FakeClient(jobs, details={"j1": "full", "j2": "full", "j3": "full"})
    analyzer = FakeAnalyzer()

    new_jobs, _ = scout._collect_new_jobs(_sources(client), storage, analyzer)

    assert analyzer.scored_refnrs == ["j1", "j2"]        # capped at 2 calls
    assert {j.refnr for j, _ in new_jobs} == {"j1", "j2"}
    assert not storage.has_job("j3")                     # deferred, unsaved


def test_llm_cap_zero_means_unlimited(monkeypatch):
    monkeypatch.setattr(scout, "MAX_LLM_CALLS_PER_RUN", 0)
    _patch_queries(monkeypatch, [{"label": "q1", "params": {}}])
    storage = FakeStorage()
    jobs = [make_job("j1"), make_job("j2"), make_job("j3")]
    client = FakeClient(jobs, details={r: "full" for r in ("j1", "j2", "j3")})
    analyzer = FakeAnalyzer()

    new_jobs, _ = scout._collect_new_jobs(_sources(client), storage, analyzer)
    assert len(new_jobs) == 3
    assert analyzer.scored_refnrs == ["j1", "j2", "j3"]


def test_validate_queries_rejects_non_list():
    with pytest.raises(SystemExit):
        scout._validate_queries({"label": "x", "params": {}})


def test_validate_queries_rejects_missing_label():
    with pytest.raises(SystemExit):
        scout._validate_queries([{"params": {}}])


def test_validate_queries_rejects_missing_params():
    with pytest.raises(SystemExit):
        scout._validate_queries([{"label": "x"}])


def test_validate_queries_accepts_valid():
    q = [
        {"label": "x", "params": {"was": "KI"}},
        {"label": "y", "params": {}, "source": "greenhouse"},
    ]
    assert scout._validate_queries(q) == q


@respx.mock
def test_ping_healthcheck_hits_url(monkeypatch):
    monkeypatch.setattr(scout, "HEALTHCHECK_URL", "https://hc.example/abc")
    route = respx.get("https://hc.example/abc").mock(return_value=httpx.Response(200))
    scout._ping_healthcheck()
    assert route.called


@respx.mock
def test_ping_healthcheck_uses_fail_suffix(monkeypatch):
    monkeypatch.setattr(scout, "HEALTHCHECK_URL", "https://hc.example/abc")
    route = respx.get("https://hc.example/abc/fail").mock(return_value=httpx.Response(200))
    scout._ping_healthcheck("/fail")
    assert route.called


def test_ping_healthcheck_noop_when_unset(monkeypatch):
    monkeypatch.setattr(scout, "HEALTHCHECK_URL", "")
    scout._ping_healthcheck()  # must not raise or make any request


def test_query_with_unknown_source_is_skipped_not_fatal(monkeypatch):
    _patch_queries(monkeypatch, [
        {"label": "stepstone query", "params": {}, "source": "stepstone"},
        {"label": "ok query", "params": {}},
    ])
    storage = FakeStorage()
    client = FakeClient([make_job("job-3")], details={"job-3": "full text"})
    analyzer = FakeAnalyzer()

    # Only "arbeitsagentur" is configured — "stepstone" isn't in `sources`.
    new_jobs, _ = scout._collect_new_jobs(_sources(client), storage, analyzer)

    assert len(new_jobs) == 1
    assert new_jobs[0][0].refnr == "job-3"
