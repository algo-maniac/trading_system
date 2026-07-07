"""Regression guard: `get_connection()` is a @contextmanager wrapping
`with psycopg.connect(...) as conn: yield conn`. Calling `.__enter__()` on it directly
(instead of `with get_connection() as conn:`) drops the context-manager reference —
CPython garbage-collects it almost immediately, which closes the still-suspended
generator and, with it, the connection (~microseconds after it opens). Every agent
entrypoint that wants a connection for its whole runtime must use `with`, not
`get_connection().__enter__()`. Caught live in strategist/trader crash-looping with
"psycopg.OperationalError: the connection is closed" on the very next statement."""
import ast
from pathlib import Path

_AGENT_APPS = [
    "src/trading_system/agents/strategist/app.py",
    "src/trading_system/agents/trader/app.py",
    "src/trading_system/agents/reader/app.py",
    "src/trading_system/agents/reader/live_app.py",
    "src/trading_system/agents/backtester/app.py",
    "src/trading_system/agents/analyst/app.py",
]

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _uses_dangerous_pattern(path: Path) -> bool:
    """True if the module calls get_connection().__enter__() rather than `with`."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr == "__enter__"
                and isinstance(func.value, ast.Call)
                and isinstance(func.value.func, ast.Name)
                and func.value.func.id == "get_connection"):
            return True
    return False


def test_no_agent_calls_get_connection_enter_directly():
    offenders = []
    for rel in _AGENT_APPS:
        path = _REPO_ROOT / rel
        if path.exists() and _uses_dangerous_pattern(path):
            offenders.append(rel)
    assert offenders == [], (
        f"{offenders} call get_connection().__enter__() directly — this closes the "
        "connection almost immediately (see module docstring). Use `with get_connection() "
        "as conn:` instead."
    )
