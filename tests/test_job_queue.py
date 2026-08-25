"""Tests for the SQLite-backed job queue."""
import time
import pytest
from api.services.job_queue import (
    JobQueue, register_job_handler, _JOB_HANDLERS,
    PENDING, RUNNING, COMPLETED, FAILED, CANCELLED,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def queue(tmp_path):
    """Create a fresh JobQueue with a temp database."""
    db = str(tmp_path / "test_jobs.db")
    q = JobQueue(db_path=db)
    return q


@pytest.fixture(autouse=True)
def _clean_handlers():
    """Save and restore handler registry between tests."""
    saved = dict(_JOB_HANDLERS)
    yield
    _JOB_HANDLERS.clear()
    _JOB_HANDLERS.update(saved)


class TestJobQueue:
    def test_enqueue_and_get(self, queue):
        job_id = queue.enqueue("test_type", params={"key": "value"})
        job = queue.get_job(job_id)
        assert job is not None
        assert job.type == "test_type"
        assert job.status == PENDING
        assert job.params == {"key": "value"}
        assert job.attempts == 0

    def test_enqueue_custom_priority(self, queue):
        id_low = queue.enqueue("a", priority=1)
        queue.enqueue("b", priority=20)
        # Lower priority number = higher priority, claimed first
        job = queue._claim_next()
        assert job.id == id_low

    def test_list_jobs_all(self, queue):
        queue.enqueue("a")
        queue.enqueue("b")
        jobs = queue.list_jobs()
        assert len(jobs) == 2

    def test_list_jobs_filter_status(self, queue):
        queue.enqueue("a")
        queue.enqueue("b")
        queue._claim_next()  # moves one to RUNNING
        assert len(queue.list_jobs(status=PENDING)) == 1
        assert len(queue.list_jobs(status=RUNNING)) == 1

    def test_list_jobs_filter_type(self, queue):
        queue.enqueue("reindex_vault")
        queue.enqueue("sync_source")
        assert len(queue.list_jobs(job_type="reindex_vault")) == 1

    def test_cancel_pending_job(self, queue):
        job_id = queue.enqueue("a")
        assert queue.cancel_job(job_id) is True
        job = queue.get_job(job_id)
        assert job.status == CANCELLED

    def test_cancel_running_job_fails(self, queue):
        job_id = queue.enqueue("a")
        queue._claim_next()  # now RUNNING
        assert queue.cancel_job(job_id) is False

    def test_claim_next_fifo(self, queue):
        id1 = queue.enqueue("a", priority=10)
        queue.enqueue("b", priority=10)
        job = queue._claim_next()
        assert job.id == id1

    def test_claim_next_empty(self, queue):
        assert queue._claim_next() is None

    def test_mark_completed(self, queue):
        job_id = queue.enqueue("a")
        queue._claim_next()
        queue._mark_completed(job_id, {"output": "done"})
        job = queue.get_job(job_id)
        assert job.status == COMPLETED
        assert job.result == {"output": "done"}
        assert job.completed_at is not None

    def test_mark_failed_with_retry(self, queue):
        job_id = queue.enqueue("a", max_attempts=3)
        queue._claim_next()
        # First failure: should go back to pending
        queue._mark_failed(job_id, "oops", attempts=1, max_attempts=3)
        job = queue.get_job(job_id)
        assert job.status == PENDING

    def test_mark_failed_exhausted(self, queue):
        job_id = queue.enqueue("a", max_attempts=2)
        queue._claim_next()
        queue._mark_failed(job_id, "oops", attempts=2, max_attempts=2)
        job = queue.get_job(job_id)
        assert job.status == FAILED
        assert job.error == "oops"

    def test_get_nonexistent_job(self, queue):
        assert queue.get_job("nonexistent") is None

    def test_to_dict(self, queue):
        job_id = queue.enqueue("a", params={"x": 1})
        job = queue.get_job(job_id)
        d = job.to_dict()
        assert d["id"] == job_id
        assert d["type"] == "a"
        assert d["params"] == {"x": 1}


class TestJobWorker:
    def test_worker_executes_job(self, queue):
        results = []

        def handler(params):
            results.append(params)
            return {"ok": True}

        register_job_handler("test_work", handler)
        job_id = queue.enqueue("test_work", params={"n": 42})

        queue.start_worker()
        # Wait for job to complete
        for _ in range(50):
            job = queue.get_job(job_id)
            if job.status == COMPLETED:
                break
            time.sleep(0.1)
        queue.stop_worker()

        assert results == [{"n": 42}]
        job = queue.get_job(job_id)
        assert job.status == COMPLETED
        assert job.result == {"ok": True}

    def test_worker_retries_on_failure(self, queue):
        call_count = [0]

        def flaky_handler(params):
            call_count[0] += 1
            if call_count[0] < 3:
                raise ValueError("fail")
            return {"attempt": call_count[0]}

        register_job_handler("flaky", flaky_handler)
        job_id = queue.enqueue("flaky", max_attempts=3)

        queue.start_worker()
        for _ in range(100):
            job = queue.get_job(job_id)
            if job.status in (COMPLETED, FAILED):
                break
            time.sleep(0.1)
        queue.stop_worker()

        job = queue.get_job(job_id)
        assert job.status == COMPLETED
        assert call_count[0] == 3

    def test_worker_marks_failed_after_max_attempts(self, queue):
        def always_fail(params):
            raise ValueError("always fails")

        register_job_handler("doomed", always_fail)
        job_id = queue.enqueue("doomed", max_attempts=2)

        queue.start_worker()
        for _ in range(100):
            job = queue.get_job(job_id)
            if job.status == FAILED:
                break
            time.sleep(0.1)
        queue.stop_worker()

        job = queue.get_job(job_id)
        assert job.status == FAILED
        assert job.attempts == 2
        assert "always fails" in job.error

    def test_worker_handles_unknown_type(self, queue):
        job_id = queue.enqueue("unknown_type", max_attempts=1)

        queue.start_worker()
        for _ in range(50):
            job = queue.get_job(job_id)
            if job.status == FAILED:
                break
            time.sleep(0.1)
        queue.stop_worker()

        job = queue.get_job(job_id)
        assert job.status == FAILED
        assert "No handler" in job.error

    def test_worker_stop(self, queue):
        queue.start_worker()
        assert queue._worker_thread.is_alive()
        queue.stop_worker()
        assert not queue._worker_thread.is_alive()

    def test_worker_processes_multiple_jobs_sequentially(self, queue):
        order = []

        def ordered_handler(params):
            order.append(params["n"])
            time.sleep(0.05)
            return {"n": params["n"]}

        register_job_handler("ordered", ordered_handler)
        queue.enqueue("ordered", params={"n": 1}, priority=10)
        queue.enqueue("ordered", params={"n": 2}, priority=10)
        queue.enqueue("ordered", params={"n": 3}, priority=10)

        queue.start_worker()
        for _ in range(100):
            jobs = queue.list_jobs(status=COMPLETED)
            if len(jobs) == 3:
                break
            time.sleep(0.1)
        queue.stop_worker()

        assert order == [1, 2, 3]
