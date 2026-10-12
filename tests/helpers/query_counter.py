"""
SQLAlchemy statement count test instrumentation.

Conforms to Phase D performance test requirements:
Counts executed SQL statements using SQLAlchemy's before_cursor_execute event
without exposing instrumentation in production payloads or creating fragile global state.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Generator, Sequence
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session


import re
import time
from collections import Counter
from dataclasses import dataclass


@dataclass
class SQLExecutionRecord:
    statement: str
    parameters: Any
    executemany: bool


class QueryCounter:
    """Records executed SQL statements and session events within a context."""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.records: list[SQLExecutionRecord] = []
        self.flush_count: int = 0
        self.commit_count: int = 0
        self.rollback_count: int = 0
        self.start_time: float = 0.0
        self.end_time: float = 0.0

    @property
    def elapsed_time(self) -> float:
        if self.end_time > 0.0:
            return self.end_time - self.start_time
        return time.perf_counter() - self.start_time

    @property
    def count(self) -> int:
        return len(self.statements)

    @property
    def select_count(self) -> int:
        return sum(1 for s in self.statements if self._normalize_stmt(s).startswith("select"))

    @property
    def insert_count(self) -> int:
        return sum(1 for s in self.statements if self._normalize_stmt(s).startswith("insert"))

    @property
    def update_count(self) -> int:
        return sum(1 for s in self.statements if self._normalize_stmt(s).startswith("update"))

    @property
    def delete_count(self) -> int:
        return sum(1 for s in self.statements if self._normalize_stmt(s).startswith("delete"))

    @property
    def executemany_count(self) -> int:
        return sum(1 for r in self.records if r.executemany)

    @staticmethod
    def _normalize_stmt(stmt: str) -> str:
        # Strip comments and normalize whitespace
        cleaned = re.sub(r"/\*.*?\*/", "", stmt, flags=re.DOTALL)
        cleaned = re.sub(r"--.*?$", "", cleaned, flags=re.MULTILINE)
        return re.sub(r"\s+", " ", cleaned).strip().lower()

    @property
    def duplicate_statements(self) -> dict[str, int]:
        """Return normalized statement templates that execute more than once."""
        counts = Counter(self._normalize_stmt(s) for s in self.statements)
        return {stmt: cnt for stmt, cnt in counts.items() if cnt > 1}

    @property
    def duplicate_select_shapes(self) -> dict[str, int]:
        """Return normalized SELECT statement templates that execute more than once."""
        counts = Counter(
            self._normalize_stmt(s) for s in self.statements
            if self._normalize_stmt(s).startswith("select")
        )
        return {stmt: cnt for stmt, cnt in counts.items() if cnt > 1}

    def summary(self) -> dict[str, Any]:
        return {
            "total_sql": self.count,
            "select": self.select_count,
            "insert": self.insert_count,
            "update": self.update_count,
            "delete": self.delete_count,
            "executemany": self.executemany_count,
            "flushes": self.flush_count,
            "commits": self.commit_count,
            "rollbacks": self.rollback_count,
            "elapsed_ms": round(self.elapsed_time * 1000, 2),
            "duplicate_selects": len(self.duplicate_select_shapes),
        }

    def reset(self) -> None:
        self.statements.clear()
        self.records.clear()
        self.flush_count = 0
        self.commit_count = 0
        self.rollback_count = 0
        self.start_time = time.perf_counter()
        self.end_time = 0.0

    def filter_statements(self, *keywords: str) -> list[str]:
        """Return subset of statements matching any of the given keywords."""
        return [
            stmt for stmt in self.statements
            if any(kw.lower() in stmt.lower() for kw in keywords)
        ]


@contextmanager
def query_counter(
    engine: Engine | None = None,
    session: Session | None = None,
) -> Generator[QueryCounter, None, None]:
    """
    Context manager to record all executed SQL statements and session events.

    Usage:
        with query_counter(session=db_session) as counter:
            action.execute(...)
        assert counter.count <= 10
    """
    target_engine = engine
    if target_engine is None and session is not None:
        target_engine = session.get_bind()
    if target_engine is None:
        from tests.conftest import test_engine
        target_engine = test_engine

    counter = QueryCounter()
    counter.start_time = time.perf_counter()

    def _before_cursor_execute(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        counter.statements.append(statement)
        counter.records.append(SQLExecutionRecord(statement, parameters, executemany))

    def _after_flush(sess: Any, flush_context: Any) -> None:
        counter.flush_count += 1

    def _after_commit(sess: Any) -> None:
        counter.commit_count += 1

    def _after_rollback(sess: Any) -> None:
        counter.rollback_count += 1

    event.listen(target_engine, "before_cursor_execute", _before_cursor_execute)
    if session is not None:
        event.listen(session, "after_flush", _after_flush)
        event.listen(session, "after_commit", _after_commit)
        event.listen(session, "after_rollback", _after_rollback)

    try:
        yield counter
    finally:
        counter.end_time = time.perf_counter()
        event.remove(target_engine, "before_cursor_execute", _before_cursor_execute)
        if session is not None:
            event.remove(session, "after_flush", _after_flush)
            event.remove(session, "after_commit", _after_commit)
            event.remove(session, "after_rollback", _after_rollback)


@contextmanager
def assert_max_queries(
    max_queries: int,
    engine: Engine | None = None,
    session: Session | None = None,
) -> Generator[QueryCounter, None, None]:
    """
    Assert that the enclosed block executes at most ``max_queries`` SQL statements.
    """
    with query_counter(engine=engine, session=session) as counter:
        yield counter
    assert counter.count <= max_queries, (
        f"Expected at most {max_queries} SQL statements, but {counter.count} executed: "
        f"{counter.statements}"
    )
