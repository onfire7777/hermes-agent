"""Tests for kanban goal_mode — per-card Ralph-style goal loop.

Covers three layers:

1. DB: goal_mode / goal_max_turns persist through create_task + from_row,
   and a legacy DB (without the columns) migrates cleanly.
2. Spawn: _default_spawn sets the HERMES_KANBAN_GOAL_MODE env vars only
   when the card opts in.
3. Loop: goals.run_kanban_goal_loop continuation / completion / budget
   behaviour, driven entirely through injected callbacks (no live model).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import goals


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------

def test_goal_mode_defaults_off(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="plain task", assignee="worker")
        task = kb.get_task(conn, tid)
    assert task.goal_mode is False
    assert task.goal_max_turns is None


def test_goal_mode_persists(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="open-ended task",
            assignee="worker",
            goal_mode=True,
            goal_max_turns=7,
        )
        task = kb.get_task(conn, tid)
    assert task.goal_mode is True
    assert task.goal_max_turns == 7


def test_goal_mode_without_max_turns(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="t", assignee="worker", goal_mode=True
        )
        task = kb.get_task(conn, tid)
    assert task.goal_mode is True
    assert task.goal_max_turns is None


def test_legacy_db_migrates_goal_columns(tmp_path, monkeypatch):
    """A tasks table created without goal columns must gain them on init."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    db_path = kb.kanban_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Minimal legacy schema: tasks table missing goal_mode / goal_max_turns.
    legacy = sqlite3.connect(db_path)
    legacy.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            assignee TEXT,
            status TEXT NOT NULL DEFAULT 'ready',
            priority INTEGER NOT NULL DEFAULT 0,
            created_by TEXT,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            workspace_path TEXT,
            claim_lock TEXT,
            claim_expires INTEGER
        )
        """
    )
    legacy.execute(
        "INSERT INTO tasks (id, title, status, priority, created_at, workspace_kind) "
        "VALUES ('legacy1', 'old', 'ready', 0, 1, 'scratch')"
    )
    legacy.commit()
    legacy.close()

    # init_db runs the additive migration.
    kb.init_db()
    with kb.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "goal_mode" in cols
        assert "goal_max_turns" in cols
        task = kb.get_task(conn, "legacy1")
    # Existing row keeps the safe default.
    assert task.goal_mode is False
    assert task.goal_max_turns is None


# ---------------------------------------------------------------------------
# Spawn env
# ---------------------------------------------------------------------------

def test_spawn_sets_goal_env_only_when_enabled(kanban_home, monkeypatch):
    captured = {}

    class _FakeProc:
        pid = 4242

    def _fake_popen(cmd, **kwargs):
        captured["env"] = kwargs.get("env", {})
        return _FakeProc()

    monkeypatch.setattr("subprocess.Popen", _fake_popen)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="goal task",
            assignee="default",
            goal_mode=True,
            goal_max_turns=5,
        )
        task = kb.get_task(conn, tid)

    kb._default_spawn(task, str(kanban_home))
    env = captured["env"]
    assert env.get("HERMES_KANBAN_GOAL_MODE") == "1"
    assert env.get("HERMES_KANBAN_GOAL_MAX_TURNS") == "5"


def test_spawn_no_goal_env_for_plain_task(kanban_home, monkeypatch):
    captured = {}

    class _FakeProc:
        pid = 4243

    def _fake_popen(cmd, **kwargs):
        captured["env"] = kwargs.get("env", {})
        return _FakeProc()

    monkeypatch.setattr("subprocess.Popen", _fake_popen)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="plain", assignee="default")
        task = kb.get_task(conn, tid)

    kb._default_spawn(task, str(kanban_home))
    env = captured["env"]
    assert "HERMES_KANBAN_GOAL_MODE" not in env
    assert "HERMES_KANBAN_GOAL_MAX_TURNS" not in env


# ---------------------------------------------------------------------------
# Goal loop logic (callback-injected, no live model)
# ---------------------------------------------------------------------------

def _patch_judge(monkeypatch, verdicts):
    """Make judge_goal return a scripted sequence of verdicts."""
    seq = list(verdicts)

    def _fake_judge(goal, response, subgoals=None, background_processes=None, **_kw):
        v = seq.pop(0) if seq else "done"
        # 4-tuple contract: (verdict, reason, parse_failed, wait_directive)
        return v, f"scripted:{v}", False, None

    monkeypatch.setattr(goals, "judge_goal", _fake_judge)


def test_loop_stops_when_worker_already_completed(monkeypatch):
    # Worker called kanban_complete on its first turn — no judging needed.
    _patch_judge(monkeypatch, ["continue"])  # should never be consulted
    turns = []

    res = goals.run_kanban_goal_loop(
        task_id="t1",
        goal_text="do the thing",
        run_turn=lambda p: turns.append(p) or "x",
        task_status_fn=lambda: "done",
        block_fn=lambda r: pytest.fail("should not block"),
        first_response="done already",
    )
    assert res["outcome"] == "completed_by_worker"
    assert turns == []  # no extra turns


def test_loop_continues_then_worker_completes(monkeypatch):
    _patch_judge(monkeypatch, ["continue", "continue"])
    statuses = iter(["running", "running", "done"])
    turns = []

    res = goals.run_kanban_goal_loop(
        task_id="t2",
        goal_text="ship feature",
        run_turn=lambda p: turns.append(p) or f"turn{len(turns)}",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda r: pytest.fail("should not block"),
        max_turns=10,
        first_response="started",
    )
    assert res["outcome"] == "completed_by_worker"
    # Two continuation turns fed before the worker completed.
    assert len(turns) == 2
    assert all("not done yet" in p for p in turns)


def test_loop_blocks_on_budget_exhaustion(monkeypatch):
    _patch_judge(monkeypatch, ["continue"] * 10)
    blocked = {}

    def _block(reason):
        blocked["reason"] = reason

    res = goals.run_kanban_goal_loop(
        task_id="t3",
        goal_text="endless task",
        run_turn=lambda p: "still going",
        task_status_fn=lambda: "running",
        block_fn=_block,
        max_turns=3,
        first_response="turn1",
    )
    assert res["outcome"] == "blocked_budget"
    assert res["turns_used"] == 3
    assert "turn budget" in blocked["reason"].lower()


def test_loop_finalize_nudge_when_judge_done_but_open(monkeypatch):
    # Judge says done, but worker never terminated → one finalize nudge,
    # then worker completes.
    _patch_judge(monkeypatch, ["done", "done"])
    statuses = iter(["running", "done"])
    turns = []

    res = goals.run_kanban_goal_loop(
        task_id="t4",
        goal_text="task",
        run_turn=lambda p: turns.append(p) or "ok",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda r: pytest.fail("should not block"),
        max_turns=10,
        first_response="looks done",
    )
    assert res["outcome"] == "completed_by_worker"
    assert len(turns) == 1
    assert "still open" in turns[0]


def test_loop_blocks_when_judge_done_but_never_finalizes(monkeypatch):
    # Judge keeps saying done, worker never calls kanban_complete → block
    # after the single finalize nudge.
    _patch_judge(monkeypatch, ["done", "done"])
    blocked = {}

    res = goals.run_kanban_goal_loop(
        task_id="t5",
        goal_text="task",
        run_turn=lambda p: "still not finalizing",
        task_status_fn=lambda: "running",
        block_fn=lambda r: blocked.update(reason=r),
        max_turns=10,
        first_response="looks done",
    )
    assert res["outcome"] == "blocked_budget"
    assert "finalize" in blocked["reason"].lower()


def test_loop_stops_if_task_reclaimed(monkeypatch):
    _patch_judge(monkeypatch, ["continue"])
    res = goals.run_kanban_goal_loop(
        task_id="t6",
        goal_text="task",
        run_turn=lambda p: pytest.fail("should not run a turn"),
        task_status_fn=lambda: "archived",
        block_fn=lambda r: pytest.fail("should not block"),
        first_response="x",
    )
    assert res["outcome"] == "stopped"


def test_loop_waits_for_process_local_session_without_spending_turn(monkeypatch):
    seen = {}
    waits = iter([True, False])
    statuses = iter(["running", "running", "running", "done"])

    def _background_processes(task_id=None, session_key=None):
        seen["task_id"] = task_id
        seen["session_key"] = session_key
        return [{"session_id": "proc_ralphex", "status": "running"}]

    monkeypatch.setattr(goals, "gather_background_processes", _background_processes)
    monkeypatch.setattr(goals, "_session_waiting", lambda _sid: next(waits))
    monkeypatch.setattr(goals.time, "sleep", lambda seconds: seen.setdefault("sleep", seconds))

    def _judge(_goal, _response, *, background_processes=None, **_kw):
        seen["background"] = background_processes
        return "wait", "RalphEx is still running", False, {"session_id": "proc_ralphex"}

    monkeypatch.setattr(goals, "judge_goal", _judge)
    turns = []
    res = goals.run_kanban_goal_loop(
        task_id="t_wait_session",
        session_key="conversation-a",
        goal_text="finish bounded RalphEx lane",
        run_turn=lambda p: turns.append(p) or "checked process output and completed",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda _r: pytest.fail("wait must not block the task"),
        max_turns=2,
        first_response="RalphEx is running in the background",
    )

    assert res["outcome"] == "completed_by_worker"
    assert res["turns_used"] == 2
    assert len(turns) == 1
    assert seen["task_id"] is None
    assert seen["session_key"] == "conversation-a"
    assert seen["background"][0]["session_id"] == "proc_ralphex"
    assert seen["sleep"] == 1.0


def test_loop_waits_for_pid_then_resumes_after_exit(monkeypatch):
    alive = iter([True, False])
    statuses = iter(["running", "running", "running", "done"])
    monkeypatch.setattr(
        goals,
        "gather_background_processes",
        lambda task_id=None, session_key=None: [
            {"pid": 4242, "status": "running", "session_key": session_key}
        ],
    )
    monkeypatch.setattr(goals, "_pid_alive", lambda _pid: next(alive))
    monkeypatch.setattr(goals.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        goals,
        "judge_goal",
        lambda *_a, **_kw: ("wait", "process running", False, {"pid": 4242}),
    )

    turns = []
    res = goals.run_kanban_goal_loop(
        task_id="t_wait_pid",
        session_key="conversation-b",
        goal_text="finish subprocess",
        run_turn=lambda p: turns.append(p) or "checked process output and completed",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda _r: pytest.fail("wait must not block the task"),
        max_turns=2,
        first_response="subprocess is running",
    )

    assert res["outcome"] == "completed_by_worker"
    assert res["turns_used"] == 2
    assert len(turns) == 1


def test_loop_rejects_wait_target_not_in_live_snapshot(monkeypatch):
    monkeypatch.setattr(
        goals,
        "gather_background_processes",
        lambda task_id=None, session_key=None: [
            {"session_id": "real", "pid": 123, "status": "running"}
        ],
    )
    monkeypatch.setattr(
        goals,
        "judge_goal",
        lambda *_a, **_kw: ("wait", "hallucinated", False, {"pid": 999}),
    )
    turns = []
    statuses = iter(["running", "done"])

    res = goals.run_kanban_goal_loop(
        task_id="t_reject_wait",
        session_key="conversation-c",
        goal_text="finish safely",
        run_turn=lambda p: turns.append(p) or "done",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda _r: pytest.fail("must resume, not block"),
        max_turns=2,
        first_response="waiting",
    )

    assert res["outcome"] == "completed_by_worker"
    assert len(turns) == 1


@pytest.mark.parametrize(
    ("terminal_status", "expected_outcome"),
    [
        ("done", "completed_by_worker"),
        ("blocked", "blocked_by_worker"),
        ("archived", "stopped"),
    ],
)
def test_loop_returns_immediately_when_task_changes_during_wait(
    monkeypatch, terminal_status, expected_outcome
):
    monkeypatch.setattr(
        goals,
        "gather_background_processes",
        lambda **_kw: [{"session_id": "proc_wait", "status": "running"}],
    )
    monkeypatch.setattr(goals, "_session_waiting", lambda _sid: True)
    monkeypatch.setattr(goals.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        goals,
        "judge_goal",
        lambda *_a, **_kw: ("wait", "running", False, {"session_id": "proc_wait"}),
    )
    statuses = iter(["running", terminal_status])

    res = goals.run_kanban_goal_loop(
        task_id="t_terminal_wait",
        session_key="conversation-terminal",
        goal_text="wait safely",
        run_turn=lambda _p: pytest.fail("terminal transition must return immediately"),
        task_status_fn=lambda: next(statuses),
        block_fn=lambda _r: pytest.fail("loop must not mutate terminal task"),
        max_turns=2,
        first_response="waiting",
    )
    assert res["outcome"] == expected_outcome
    assert res["turns_used"] == 1


@pytest.mark.parametrize(
    ("terminal_status", "expected_outcome"),
    [
        ("done", "completed_by_worker"),
        ("blocked", "blocked_by_worker"),
        ("archived", "stopped"),
    ],
)
def test_loop_rechecks_task_when_wait_target_is_already_released(
    monkeypatch, terminal_status, expected_outcome
):
    monkeypatch.setattr(
        goals,
        "gather_background_processes",
        lambda **_kw: [{"session_id": "proc_released", "status": "running"}],
    )
    monkeypatch.setattr(goals, "_session_waiting", lambda _sid: False)
    monkeypatch.setattr(
        goals,
        "judge_goal",
        lambda *_a, **_kw: (
            "wait",
            "process just released",
            False,
            {"session_id": "proc_released"},
        ),
    )
    statuses = iter(["running", terminal_status])

    res = goals.run_kanban_goal_loop(
        task_id="t_release_race",
        session_key="conversation-release",
        goal_text="finish without racing",
        run_turn=lambda _p: pytest.fail("terminal task must not consume a turn"),
        task_status_fn=lambda: next(statuses),
        block_fn=lambda _r: pytest.fail("loop must not mutate terminal task"),
        max_turns=2,
        first_response="waiting",
    )
    assert res["outcome"] == expected_outcome
    assert res["turns_used"] == 1


def test_loop_session_watch_trigger_releases_wait(monkeypatch):
    waiting = iter([True, False])
    statuses = iter(["running", "running", "running", "done"])
    monkeypatch.setattr(
        goals,
        "gather_background_processes",
        lambda **_kw: [{
            "session_id": "proc_watch",
            "status": "running",
            "watch_patterns": ["READY"],
        }],
    )
    monkeypatch.setattr(goals, "_session_waiting", lambda _sid: next(waiting))
    monkeypatch.setattr(goals.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        goals,
        "judge_goal",
        lambda *_a, **_kw: ("wait", "watching", False, {"session_id": "proc_watch"}),
    )
    turns = []
    res = goals.run_kanban_goal_loop(
        task_id="t_watch",
        session_key="conversation-watch",
        goal_text="wait for READY",
        run_turn=lambda p: turns.append(p) or "finalized",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda _r: pytest.fail("must not block"),
        max_turns=2,
        first_response="watcher running",
    )
    assert res["outcome"] == "completed_by_worker"
    assert len(turns) == 1


def test_cli_wrapper_forwards_stable_session_id(kanban_home, monkeypatch):
    import cli as cli_mod

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="goal task",
            body="finish it",
            assignee="default",
            goal_mode=True,
        )
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    captured = {}
    monkeypatch.setattr(
        goals,
        "run_kanban_goal_loop",
        lambda **kwargs: captured.update(kwargs),
    )
    turns = []

    class _Agent:
        def run_conversation(self, **kwargs):
            from tools.approval import get_current_session_key

            turns.append((get_current_session_key(default=""), kwargs))
            return {"final_response": "continued"}

    fake_cli = type(
        "FakeCLI",
        (),
        {
            "session_id": "stable-cli-session",
            "conversation_history": [],
            "agent": _Agent(),
        },
    )()

    cli_mod._run_kanban_goal_loop_q(fake_cli, "started")
    assert captured["task_id"] == task_id
    assert captured["session_key"] == "stable-cli-session"
    captured["run_turn"]("continue")
    assert turns[0][0] == "stable-cli-session"
    assert turns[0][1]["task_id"] == "stable-cli-session"
