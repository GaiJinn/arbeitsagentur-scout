from arbeitsagentur import Job
from analyzer import JobScore
from storage import JobStorage


def make_job(refnr: str = "ref-1") -> Job:
    return Job(
        refnr=refnr,
        title="Werkstudent Software",
        employer="Beispiel GmbH",
        location="40474 Düsseldorf",
        posted_date="2026-06-01",
        profession="Informatiker",
        url="https://example.com/job",
        description="Beispielbeschreibung",
    )


def test_has_job_false_for_unseen(tmp_path):
    storage = JobStorage(tmp_path / "jobs.db")
    assert storage.has_job("unknown") is False


def test_save_and_has_job_round_trip(tmp_path):
    storage = JobStorage(tmp_path / "jobs.db")
    job = make_job()
    storage.save(job, score=None)
    assert storage.has_job(job.refnr) is True


def test_save_persists_score_fields(tmp_path):
    storage = JobStorage(tmp_path / "jobs.db")
    job = make_job()
    score = JobScore(
        score=8,
        summary="Guter Fit",
        key_skills=["Python", "Docker"],
        fit_reasons=["Skill-Match"],
        flags=["Befristet"],
    )
    storage.save(job, score)

    row = storage.conn.execute(
        "SELECT score, summary, key_skills FROM jobs WHERE refnr = ?", (job.refnr,)
    ).fetchone()
    assert row[0] == 8
    assert row[1] == "Guter Fit"
    assert "Python" in row[2]


def test_save_without_score_stores_nulls(tmp_path):
    storage = JobStorage(tmp_path / "jobs.db")
    job = make_job()
    storage.save(job, score=None)

    row = storage.conn.execute(
        "SELECT score, summary FROM jobs WHERE refnr = ?", (job.refnr,)
    ).fetchone()
    assert row[0] is None
    assert row[1] is None


def test_save_is_idempotent_on_refnr(tmp_path):
    storage = JobStorage(tmp_path / "jobs.db")
    job = make_job()
    storage.save(job, score=None)
    storage.save(job, score=None)

    count = storage.conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE refnr = ?", (job.refnr,)
    ).fetchone()[0]
    assert count == 1


def test_db_path_persists_across_instances(tmp_path):
    db_path = tmp_path / "jobs.db"
    JobStorage(db_path).save(make_job("ref-persist"), score=None)

    reopened = JobStorage(db_path)
    assert reopened.has_job("ref-persist") is True


def test_get_job_returns_none_for_unknown(tmp_path):
    storage = JobStorage(tmp_path / "jobs.db")
    assert storage.get_job("unknown") is None


def test_get_job_returns_dict_with_score_fields(tmp_path):
    storage = JobStorage(tmp_path / "jobs.db")
    job = make_job()
    score = JobScore(score=9, summary="Top fit", key_skills=["Python"])
    storage.save(job, score)

    row = storage.get_job(job.refnr)
    assert row["refnr"] == job.refnr
    assert row["title"] == job.title
    assert row["employer"] == job.employer
    assert row["score"] == 9
    assert row["summary"] == "Top fit"


def test_bot_state_round_trip(tmp_path):
    storage = JobStorage(tmp_path / "jobs.db")
    assert storage.get_bot_state("offset") is None
    assert storage.get_bot_state("offset", default="0") == "0"

    storage.set_bot_state("offset", "42")
    assert storage.get_bot_state("offset") == "42"

    storage.set_bot_state("offset", "43")
    assert storage.get_bot_state("offset") == "43"
