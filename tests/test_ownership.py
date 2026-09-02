"""Who holds a claim, and when someone else may take it.

The queue is shared between `bq`, the daemon, and any Claude session draining
inline. A bare "[~]" says a task was claimed but not by what, so it cannot be
told apart from one a dead worker left behind — these tests pin down the
attribution and the staleness rule built to replace that guess.
"""

import time

import pytest
from buffer_queue import STALE_AFTER, Queue


@pytest.fixture
def queued(qpath):
    def _add(*texts):
        with Queue(qpath) as q:
            return [q.add(t) for t in texts]
    return _add


def age_claim(qpath, task_id, seconds):
    """Backdate a heartbeat, as if the worker had gone quiet that long ago."""
    with Queue(qpath) as q:
        q.find(task_id)["hb"] = int(time.time()) - seconds
        q.save()


def test_claim_records_owner_and_heartbeat(qpath, queued):
    queued("one")
    with Queue(qpath) as q:
        task = q.claim("worker-a")
    assert task["worker"] == "worker-a"
    assert task["hb"] >= int(time.time()) - 5
    assert "worker=worker-a" in qpath.read_text(encoding="utf-8")


def test_reset_leaves_a_live_claim_alone(qpath, queued):
    """The bug this whole mechanism exists for: a blanket reset used to drag
    another worker's in-flight task back to pending underneath it."""
    queued("one")
    with Queue(qpath) as q:
        task = q.claim("worker-a")

    with Queue(qpath) as q:
        assert q.reset_running(STALE_AFTER) == 0

    with Queue(qpath) as q:
        assert q.find(task["id"])["status"] == "running"
        assert q.find(task["id"])["worker"] == "worker-a"


def test_reset_reclaims_a_silent_claim(qpath, queued):
    queued("one")
    with Queue(qpath) as q:
        task = q.claim("worker-a")
    age_claim(qpath, task["id"], STALE_AFTER + 60)

    with Queue(qpath) as q:
        assert q.reset_running(STALE_AFTER) == 1

    with Queue(qpath) as q:
        got = q.find(task["id"])
    assert got["status"] == "pending"
    assert got["worker"] is None and got["hb"] is None


def test_heartbeat_keeps_a_long_task(qpath, queued):
    queued("one")
    with Queue(qpath) as q:
        task = q.claim("worker-a")
    age_claim(qpath, task["id"], STALE_AFTER + 60)

    with Queue(qpath) as q:
        q.heartbeat(task["id"], "worker-a")
    with Queue(qpath) as q:
        assert q.reset_running(STALE_AFTER) == 0


def test_heartbeat_ignores_tasks_that_are_not_running(qpath, queued):
    (task,) = queued("one")
    with Queue(qpath) as q:
        assert q.heartbeat(task["id"]) is None
        assert q.find(task["id"])["hb"] is None


def test_force_takes_everything_back(qpath, queued):
    queued("one", "two")
    with Queue(qpath) as q:
        q.claim("worker-a")
        q.claim("worker-b")
    with Queue(qpath) as q:
        assert q.reset_running(force=True) == 2


def test_finishing_a_task_releases_the_claim(qpath, queued):
    queued("one")
    with Queue(qpath) as q:
        task = q.claim("worker-a")
        q.set_status(task["id"], "done")
        got = q.find(task["id"])
    assert got["worker"] is None and got["hb"] is None


def test_a_claim_with_no_heartbeat_at_all_is_stale(qpath, queued):
    """Rows written before ownership existed, or hand-edited to [~]."""
    queued("one")
    with Queue(qpath) as q:
        task = q.claim("worker-a")
        q.find(task["id"])["hb"] = None
        q.save()
    with Queue(qpath) as q:
        assert q.reset_running(STALE_AFTER) == 1


def test_ownership_survives_a_round_trip_with_other_fields(qpath, queued):
    (task,) = queued("do a thing # with a hash")
    with Queue(qpath) as q:
        q.claim("host.local:4321")
        q.set_session(task["id"], "sess-9")
    with Queue(qpath) as q:
        got = q.find(task["id"])
    assert got["text"] == "do a thing # with a hash"
    assert got["worker"] == "host.local:4321"
    assert got["session"] == "sess-9"
    assert got["status"] == "running"
