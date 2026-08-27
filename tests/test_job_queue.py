"""Tests for the SQLite-backed job queue."""
import logging
import time
import pytest
from api.services.job_queue import (
    JobQueue, register_job_handler, _JOB_HANDLERS,
    PENDING, RUNNING, COMPLETED, FAILED, CANCELLED,
    is_stale_running_job,
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


def _simulate_new_process(queue):
    """Bump process_start_time to the far future, so a job already claimed
    on `queue` (started_at set at __init__ time, necessarily earlier) reads
    as belonging to a since-restarted previous process rather than this
    one — the real-world ordering reconciliation depends on (#768 Codex
    review: reconciliation is now scoped to started_at < process_start_time,
    so simulating "stale" requires the timestamps in this relative order,
    not just reusing the same queue object to claim-then-reconcile)."""
    queue.process_start_time = "9999-01-01T00:00:00+00:00"


class TestOrphanReconciliation:
    """A job left RUNNING by a process that restarted mid-job is never
    revisited by anything else in this module — cleanup_old_jobs() only
    touches COMPLETED/FAILED/CANCELLED rows. start_worker() must reconcile
    it before the (new) worker thread starts (#768)."""

    def test_start_worker_reconciles_stale_running_job(self, queue):
        job_id = queue.enqueue("a")
        queue._claim_next()  # simulate a previous process having left this RUNNING
        assert queue.get_job(job_id).status == RUNNING
        _simulate_new_process(queue)

        queue.start_worker()
        queue.stop_worker()

        job = queue.get_job(job_id)
        assert job.status == FAILED
        assert "orphan" in job.error.lower()
        assert job.completed_at is not None

    def test_reconciliation_does_not_touch_a_job_from_this_same_process(self, queue):
        """A RUNNING row whose started_at is AFTER this process's own start
        cannot have been stranded by a restart of this process — reconciling
        it unconditionally (rather than scoping to process_start_time) would
        risk killing a job a still-alive process is genuinely executing
        against the same database (Codex review of #768)."""
        job_id = queue.enqueue("a")
        queue._claim_next()
        # No _simulate_new_process() call: process_start_time (set at
        # __init__, before enqueue/claim above) predates started_at, so this
        # job reads as belonging to this process's own current lifetime.

        queue._reconcile_orphaned_jobs()

        assert queue.get_job(job_id).status == RUNNING

    def test_reconciliation_does_not_offer_a_retry(self, queue):
        """Unlike _mark_failed(), reconciliation always lands on FAILED —
        the job's owning process is gone, not merely one attempt, so it must
        not be silently re-queued to PENDING regardless of attempts left."""
        job_id = queue.enqueue("a", max_attempts=5)
        queue._claim_next()
        _simulate_new_process(queue)
        queue.start_worker()
        queue.stop_worker()
        assert queue.get_job(job_id).status == FAILED

    def test_start_worker_does_not_touch_completed_jobs(self, queue):
        job_id = queue.enqueue("a")
        queue._claim_next()
        queue._mark_completed(job_id, {"ok": True})
        _simulate_new_process(queue)

        queue.start_worker()
        queue.stop_worker()

        job = queue.get_job(job_id)
        assert job.status == COMPLETED
        assert job.result == {"ok": True}

    def test_reconciliation_does_not_touch_pending_jobs(self, queue):
        # Reconciliation runs synchronously in start_worker(), before the
        # worker thread is spawned, so calling it directly (as the
        # acceptance criteria's "equivalent explicit reconciliation call")
        # avoids racing against the worker loop picking this job up itself.
        job_id = queue.enqueue("a")
        _simulate_new_process(queue)
        queue._reconcile_orphaned_jobs()
        assert queue.get_job(job_id).status == PENDING

    def test_no_reconciliation_when_nothing_running(self, queue, caplog):
        queue.enqueue("a")  # stays PENDING
        _simulate_new_process(queue)
        with caplog.at_level(logging.WARNING):
            queue.start_worker()
        queue.stop_worker()
        assert "Reconciled" not in caplog.text

    def test_reconciliation_logs_a_warning(self, queue, caplog):
        queue.enqueue("a")
        queue._claim_next()
        _simulate_new_process(queue)
        with caplog.at_level(logging.WARNING):
            queue.start_worker()
        queue.stop_worker()
        assert "Reconciled 1 orphaned job" in caplog.text

    def test_reconciliation_treats_missing_started_at_as_stale(self, queue):
        """A RUNNING row with no started_at can't be a real live claim —
        _claim_next always sets it in the same atomic UPDATE — so it must
        still be reconciled even though it fails the started_at < process_
        start_time comparison outright (#768 Codex review)."""
        job_id = queue.enqueue("a")
        queue._claim_next()
        with queue._conn() as conn:
            conn.execute("UPDATE jobs SET started_at = NULL WHERE id = ?", (job_id,))
        # Deliberately NOT calling _simulate_new_process(): even with
        # process_start_time unchanged, a null started_at must not survive
        # via the `started_at < process_start_time` comparison alone.

        queue._reconcile_orphaned_jobs()

        assert queue.get_job(job_id).status == FAILED


class TestStaleRunningJobHelper:
    """is_stale_running_job() — the signal admin/jobs routes use to flag
    staleness before start_worker()'s reconciliation has had a chance to
    run, or against a queue that never called start_worker() at all (#768)."""

    def test_true_when_started_before_process_start(self, queue):
        job_id = queue.enqueue("a")
        queue._claim_next()
        job = queue.get_job(job_id)
        # process_start_time is set at __init__, before enqueue/claim above,
        # so started_at (set by _claim_next, after __init__) normally sorts
        # *after* it -- force the stale case explicitly instead.
        future_process_start = "9999-01-01T00:00:00+00:00"
        assert is_stale_running_job(job, future_process_start) is True

    def test_false_when_started_after_process_start(self, queue):
        job_id = queue.enqueue("a")
        queue._claim_next()
        job = queue.get_job(job_id)
        past_process_start = "0001-01-01T00:00:00+00:00"
        assert is_stale_running_job(job, past_process_start) is False

    def test_false_when_not_running(self, queue):
        job_id = queue.enqueue("a")
        queue._claim_next()
        queue._mark_completed(job_id, {"ok": True})
        job = queue.get_job(job_id)
        future_process_start = "9999-01-01T00:00:00+00:00"
        assert is_stale_running_job(job, future_process_start) is False
