"""Admission tests for the host-aware Kanban dispatch cap."""

import argparse
import contextlib
import json

from hermes_cli import kanban_db as kb


def test_adaptive_dispatch_cap_fails_closed_on_host_pressure(monkeypatch):
    monkeypatch.setattr(kb, "_free_memory_percent", lambda: 29.0)
    monkeypatch.setattr(kb.os, "getloadavg", lambda: (12.0, 0.0, 0.0))

    assert kb.adaptive_dispatch_cap(6, enabled=True) == 0


def test_adaptive_dispatch_cap_widens_only_as_resources_allow(monkeypatch):
    cases = [
        ((40.0, 9.0), 3),
        ((50.0, 7.0), 4),
        ((60.0, 5.0), 6),
    ]
    for (free, load), expected in cases:
        monkeypatch.setattr(kb, "_free_memory_percent", lambda free=free: free)
        monkeypatch.setattr(
            kb.os, "getloadavg", lambda load=load: (load, 0.0, 0.0)
        )
        assert kb.adaptive_dispatch_cap(6, enabled=True) == expected


def test_adaptive_dispatch_cap_preserves_disabled_and_lower_configured_caps():
    assert kb.adaptive_dispatch_cap(2, enabled=False) == 2


def test_dispatch_result_records_intentional_admission_pause(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb.init_db()
    monkeypatch.setattr(kb, "_free_memory_percent", lambda: 29.0)
    monkeypatch.setattr(kb.os, "getloadavg", lambda: (12.0, 0.0, 0.0))

    with kb.connect_closing() as conn:
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *_args: 1,
            max_spawn=6,
            adaptive_max_spawn=True,
        )

    assert result.adaptive_admission_paused is True
    assert result.adaptive_admission_reason == (
        "free memory 29.0% < 35.0%; load1 12.0 > 10.0"
    )


def test_cli_health_treats_admission_pause_as_healthy():
    from hermes_cli import kanban

    paused = kb.DispatchResult(
        adaptive_admission_paused=True,
        adaptive_admission_reason="free memory 29.0% < 35.0%",
    )
    assert kanban._dispatcher_tick_is_bad(paused, ready_pending=True) is False

    unpaused = kb.DispatchResult()
    assert kanban._dispatcher_tick_is_bad(unpaused, ready_pending=True) is True


def test_gateway_health_treats_only_admission_pause_as_healthy():
    from gateway.kanban_watchers import _dispatcher_tick_is_bad

    paused = kb.DispatchResult(adaptive_admission_paused=True)
    assert _dispatcher_tick_is_bad(
        [paused], ready_pending=True, any_spawned=False
    ) is False
    assert _dispatcher_tick_is_bad(
        [kb.DispatchResult()], ready_pending=True, any_spawned=False
    ) is True


def test_cli_dispatch_exposes_admission_pause_in_json_and_text(
    monkeypatch, capsys
):
    from hermes_cli import kanban

    result = kb.DispatchResult(
        adaptive_admission_paused=True,
        adaptive_admission_reason="free memory 29.0% < 35.0%",
    )

    @contextlib.contextmanager
    def fake_connect():
        yield object()

    monkeypatch.setattr(kanban.kb, "connect_closing", fake_connect)
    monkeypatch.setattr(kanban.kb, "dispatch_once", lambda *_a, **_kw: result)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"adaptive_max_spawn": True, "max_spawn": 6}},
    )

    args = argparse.Namespace(dry_run=True, max=None, failure_limit=2, json=True)
    assert kanban._cmd_dispatch(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["adaptive_admission_paused"] is True
    assert payload["adaptive_admission_reason"] == (
        "free memory 29.0% < 35.0%"
    )

    args.json = False
    assert kanban._cmd_dispatch(args) == 0
    assert (
        "Adaptive admission paused: free memory 29.0% < 35.0%"
        in capsys.readouterr().out
    )
