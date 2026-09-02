"""Queue file format and state transitions."""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from buffer_queue import FORMAT_MARKER, Queue

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "buffer_queue.py"


def add(path, *texts):
    with Queue(path) as q:
        return [q.add(t) for t in texts]


def read(path):
    with Queue(path) as q:
        return q.tasks


# -- format: text is verbatim to end of line -------------------------------
#
# Regression for the bug where " # " in a task was read as the note separator
# and the rest of the task was silently dropped on the way to `claude -p`.


def test_hash_in_task_text_is_preserved(qpath):
    text = "run make # then check the log"
    add(qpath, text)
    assert read(qpath)[0]["text"] == text


def test_issue_reference_is_preserved(qpath):
    text = "fix #214 in axi_fifo.sv"
    add(qpath, text)
    assert read(qpath)[0]["text"] == text


def test_pipe_in_task_text_is_preserved(qpath):
    text = "run tests | tee out.log # and keep it"
    add(qpath, text)
    assert read(qpath)[0]["text"] == text


def test_note_containing_hash_and_space_round_trips(qpath):
    (task,) = add(qpath, "do a thing")
    note = "exit 1: no such target # oops"
    with Queue(qpath) as q:
        q.set_status(task["id"], "failed", note)
    got = read(qpath)[0]
    assert got["note"] == note
    assert got["text"] == "do a thing"


def test_unicode_survives(qpath):
    text = "check ✓ 日本語 output"
    add(qpath, text)
    assert read(qpath)[0]["text"] == text


def test_text_that_looks_like_a_queue_line(qpath):
    text = "- [x] id=deadbeef ts=2026-01-01T00:00:00Z | not a real row"
    add(qpath, text)
    tasks = read(qpath)
    assert len(tasks) == 1
    assert tasks[0]["text"] == text


# -- format 1 files still load, and migrate on write -----------------------


V1 = (
    "# Buffer queue\n\n"
    "- [ ] id=aaaa1111 ts=2026-08-30T10:00:00Z | old pending task\n"
    "- [!] id=bbbb2222 ts=2026-08-30T10:01:00Z | old failed # timed out\n"
)


def test_v1_file_is_read_with_v1_semantics(qpath):
    qpath.parent.mkdir(parents=True, exist_ok=True)
    qpath.write_text(V1, encoding="utf-8")
    tasks = read(qpath)
    assert [t["text"] for t in tasks] == ["old pending task", "old failed"]
    assert tasks[1]["note"] == "timed out"


def test_v1_file_migrates_on_first_write(qpath):
    qpath.parent.mkdir(parents=True, exist_ok=True)
    qpath.write_text(V1, encoding="utf-8")
    add(qpath, "new task # with a hash")

    assert FORMAT_MARKER in qpath.read_text(encoding="utf-8")
    tasks = read(qpath)
    assert [t["text"] for t in tasks] == [
        "old pending task",
        "old failed",
        "new task # with a hash",
    ]
    assert tasks[1]["note"] == "timed out"


# -- ordering and transitions ---------------------------------------------


def test_claim_is_fifo(qpath):
    add(qpath, "one", "two", "three")
    with Queue(qpath) as q:
        assert q.claim()["text"] == "one"
    with Queue(qpath) as q:
        assert q.claim()["text"] == "two"


def test_reset_running_requeues_only_running(qpath):
    tasks = add(qpath, "one", "two", "three")
    with Queue(qpath) as q:
        q.claim()
        q.set_status(tasks[2]["id"], "done")
    with Queue(qpath) as q:
        assert q.reset_running() == 1
    statuses = [t["status"] for t in read(qpath)]
    assert statuses == ["pending", "pending", "done"]


def test_clear_failed_keeps_pending(qpath):
    tasks = add(qpath, "one", "two", "three")
    with Queue(qpath) as q:
        q.set_status(tasks[0]["id"], "done")
        q.set_status(tasks[1]["id"], "failed", "nope")
    with Queue(qpath) as q:
        assert q.clear({"done", "failed"}) == 2
    assert [t["text"] for t in read(qpath)] == ["three"]


# -- the lock is what keeps two workers off one task -----------------------


def test_concurrent_claims_never_hand_out_the_same_task(qpath):
    """Real processes, real file locks — threads would share a file table."""
    n = 6
    add(qpath, *[f"task{i}" for i in range(n)])

    def claim():
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--file", str(qpath), "--json", "claim"],
            capture_output=True, text=True,
        )
        return json.loads(proc.stdout)["id"]

    with ThreadPoolExecutor(max_workers=n) as pool:
        ids = list(pool.map(lambda _: claim(), range(n)))

    assert len(set(ids)) == n, f"a task was claimed twice: {ids}"
    assert all(t["status"] == "running" for t in read(qpath))


# -- CLI mutations report missing IDs -------------------------------------


def test_mutating_commands_on_unknown_id_exit_nonzero_and_report_id(qpath):
    add(qpath, "sample task")

    for cmd in ["done", "fail", "requeue", "remove"]:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--file", str(qpath), cmd, "deadbeef"],
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0
        assert "deadbeef" in proc.stderr
        assert "no such task" in proc.stderr


def test_mutating_commands_on_known_id_succeed(qpath):
    (task,) = add(qpath, "sample task")
    tid = task["id"]

    # requeue
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--file", str(qpath), "requeue", tid],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert tid in proc.stdout

    # fail
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--file", str(qpath), "fail", tid, "--note", "oops"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert tid in proc.stdout

    # done
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--file", str(qpath), "done", tid],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert tid in proc.stdout

    # remove
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--file", str(qpath), "--json", "remove", tid],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {"removed": True}

