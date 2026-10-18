"""
Every row lock runs inside a transaction.

``select_for_update`` outside a transaction is an error on MySQL and Postgres,
but SQLite — which the tests run on — ignores the lock entirely, so a
behavioural test passes either way. That is exactly how a misplaced
``@transaction.atomic`` (on the helper above ``confirm_loan_repayment`` instead
of on it) reached production: every online loan repayment was taken by
Paystack and then failed to confirm with a 500.

So this checks the code itself: any function that calls ``select_for_update``
must be decorated ``@transaction.atomic`` or open ``with transaction.atomic()``.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP = {"tests", "migrations", ".venv", "venv", "node_modules", "frontend_dist"}


def _source_files():
    for path in ROOT.rglob("*.py"):
        if not SKIP.intersection(path.relative_to(ROOT).parts):
            yield path


def _is_atomic(node) -> bool:
    return "atomic" in ast.unparse(node)


def _unguarded_locks():
    found = []
    for path in _source_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if "select_for_update" not in ast.unparse(fn):
                continue
            decorated = any(_is_atomic(d) for d in fn.decorator_list)
            in_block = any(
                isinstance(n, ast.With)
                and any(_is_atomic(item.context_expr) for item in n.items)
                for n in ast.walk(fn))
            if not (decorated or in_block):
                found.append(f"{path.relative_to(ROOT)}:{fn.lineno} {fn.name}")
    return found


def test_every_select_for_update_is_inside_a_transaction():
    assert _unguarded_locks() == []


def test_the_check_finds_the_functions_it_should():
    """Guard the guard: if the scan matched nothing it would pass vacuously."""
    from loans.services import confirm_loan_repayment  # noqa: F401

    scanned = [p for p in _source_files() if p.name == "services.py"]
    assert any("select_for_update" in p.read_text() for p in scanned)
