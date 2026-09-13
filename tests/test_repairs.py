import json
import re
import sys

import pytest
import repairs


def write(tmp_path, rules):
    (tmp_path / "repairs.json").write_text(json.dumps({"rules": rules}), encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    monkeypatch.delenv("CLAUDE_BUFFER_REPAIRS", raising=False)


def test_no_config_is_no_rules(tmp_path):
    assert repairs.load(tmp_path) == []


def test_env_overrides_location(tmp_path, monkeypatch):
    elsewhere = tmp_path / "custom.json"
    elsewhere.write_text(json.dumps([{"match": "x", "run": ["true"]}]), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_BUFFER_REPAIRS", str(elsewhere))
    assert len(repairs.load(tmp_path)) == 1


def test_broken_config_warns_and_keeps_going(tmp_path):
    (tmp_path / "repairs.json").write_text("{not json", encoding="utf-8")
    warnings = []
    assert repairs.load(tmp_path, warnings.append) == []
    assert warnings


def test_bad_rule_is_skipped_but_others_load(tmp_path):
    write(tmp_path, [
        {"name": "broken", "match": "[unclosed", "run": ["true"]},
        {"name": "ok", "match": "no device attached", "run": ["true"]},
    ])
    warnings = []
    rules = repairs.load(tmp_path, warnings.append)
    assert [r.name for r in rules] == ["ok"]
    assert warnings


def test_match_is_case_insensitive_and_first_wins(tmp_path):
    write(tmp_path, [
        {"name": "first", "match": "no device attached", "run": ["true"]},
        {"name": "second", "match": "device", "run": ["true"]},
    ])
    rules = repairs.load(tmp_path)
    assert repairs.match(rules, "❌ No device attached. Start the emulator").name == "first"
    assert repairs.match(rules, "all good") is None


def test_apply_runs_the_command(tmp_path):
    rule = repairs.Rule(name="echo", pattern=re.compile("x"),
                        run=[sys.executable, "-c", "print('fixed')"])
    ok, out = repairs.apply(rule)
    assert ok and "fixed" in out


def test_apply_reports_a_missing_command(tmp_path):
    rule = repairs.Rule(name="nope", pattern=re.compile("x"),
                        run=["definitely-not-a-real-binary-xyz"])
    ok, out = repairs.apply(rule)
    assert not ok and out


# --- the drain loop's side of it -------------------------------------------

import argparse  # noqa: E402

import drain  # noqa: E402
from buffer_queue import Queue  # noqa: E402


class ScriptedCLI:
    def __init__(self, *script):
        self.script = list(script)
        self.calls = 0

    def __call__(self, text, cli, extra, timeout, resume, use_api_key=False, cwd=None):
        self.calls += 1
        step = self.script.pop(0) if self.script else {"mode": "ok"}
        if step["mode"] == "fail":
            return False, step.get("out", "boom"), "sess-1", step.get("out", "boom")
        return True, "ok", "sess-1", ""


def drain_args(**over):
    base = dict(
        watch=False, poll=0, cli="claude", claude_arg=[], timeout=60,
        chain=False, fallback_api_key=False, max_retries=3,
        max_sleep=drain.DEFAULT_MAX_SLEEP, stale_after=drain.STALE_AFTER,
        checkpoint=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


@pytest.fixture
def rules_beside_the_queue(qpath, tmp_path, monkeypatch):
    """Point the repair config at a marker-writing command."""
    marker = tmp_path / "repaired"
    drain.use_state_for(qpath)
    cfg = repairs.config_path(drain.state_dir())
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"rules": [{
        "name": "android emulator",
        "match": "no device attached",
        "run": [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('1')"],
    }]}), encoding="utf-8")
    return marker


def test_a_precondition_failure_runs_the_repair_and_retries(
    qpath, rules_beside_the_queue, monkeypatch
):
    with Queue(qpath) as q:
        q.add("run the 05:00 sweep")
    fake = ScriptedCLI(
        {"mode": "fail", "out": "No device attached. Start the emulator"},
        {"mode": "ok"},
    )
    monkeypatch.setattr(drain, "run_task", fake)
    monkeypatch.setattr(drain.time, "sleep", lambda _: None)

    drain.drain(drain_args(), qpath)

    assert rules_beside_the_queue.exists(), "the repair command never ran"
    assert fake.calls == 2, "the task was not retried after the repair"
    with Queue(qpath) as q:
        assert q.tasks[0]["status"] == "done"


def test_the_repair_fires_once_and_a_real_failure_still_fails(
    qpath, rules_beside_the_queue, monkeypatch
):
    fail = {"mode": "fail", "out": "No device attached."}
    with Queue(qpath) as q:
        q.add("run the 05:00 sweep")
    fake = ScriptedCLI(fail, fail, fail, fail, fail)
    monkeypatch.setattr(drain, "run_task", fake)
    monkeypatch.setattr(drain.time, "sleep", lambda _: None)

    drain.drain(drain_args(max_retries=2), qpath)

    # One repaired retry, then the ordinary two attempts' worth of retries.
    assert fake.calls == 3
    with Queue(qpath) as q:
        assert q.tasks[0]["status"] == "failed"


def test_an_unrelated_failure_does_not_run_any_repair(
    qpath, rules_beside_the_queue, monkeypatch
):
    with Queue(qpath) as q:
        q.add("run the 05:00 sweep")
    monkeypatch.setattr(drain, "run_task", ScriptedCLI({"mode": "fail", "out": "syntax error"}))
    monkeypatch.setattr(drain.time, "sleep", lambda _: None)

    drain.drain(drain_args(max_retries=1), qpath)

    assert not rules_beside_the_queue.exists()
