"""Analyst entrypoint resilience: each cycle runs on a FRESH DB connection, and a
cycle failure is caught + logged (never propagated, never silent) so a dropped
connection can't wedge the agent (regression for the 2026-07-05 silent wedge)."""
from contextlib import contextmanager
from unittest.mock import patch

from trading_system.agents.analyst import app


def test_run_cycle_fresh_opens_a_new_connection_and_core_each_call():
    opened = []

    @contextmanager
    def fake_conn():
        conn = object()
        opened.append(conn)
        yield conn

    with patch.object(app, "get_connection", fake_conn), \
         patch.object(app, "build_core", return_value="CORE") as build, \
         patch.object(app, "run_cycle", return_value={"status": "ok"}) as run:
        app.run_cycle_fresh()
        app.run_cycle_fresh()

    assert len(opened) == 2                       # a fresh connection per cycle
    assert build.call_count == 2                  # core rebuilt each time (cap is DB-derived)
    assert run.call_count == 2


def test_run_cycle_fresh_swallows_db_failure_and_reports_error():
    # A dead connection must fail ONE cycle loudly, not raise out of the scheduled job
    # (which stayed alive on a dead handle and silently wedged the old design).
    with patch.object(app, "get_connection", side_effect=RuntimeError("db connection lost")):
        result = app.run_cycle_fresh()
    assert result["status"] == "error"
    assert "db connection lost" in result["error"]


def test_run_cycle_fresh_recovers_on_the_next_cycle():
    # First cycle's connection drops; the SECOND opens fresh and succeeds — this is the
    # self-heal the per-cycle connection buys (the next scheduled tick reconnects).
    calls = {"n": 0}

    @contextmanager
    def flaky_conn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db down")
        yield object()

    with patch.object(app, "get_connection", flaky_conn), \
         patch.object(app, "build_core", return_value="CORE"), \
         patch.object(app, "run_cycle", return_value={"status": "ok"}):
        first = app.run_cycle_fresh()
        second = app.run_cycle_fresh()

    assert first["status"] == "error"
    assert second["status"] == "ok"
