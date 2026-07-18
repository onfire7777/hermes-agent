"""Admission tests for the host-aware Kanban dispatch cap."""

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
