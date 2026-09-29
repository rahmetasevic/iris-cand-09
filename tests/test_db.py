from __future__ import annotations

import psycopg
import pytest

from iris_pilot import db
from iris_pilot.config import DatabaseSettings, Secret
from iris_pilot.db import DatabaseUnavailable, wait_for_database

SETTINGS = DatabaseSettings(host="db", port=5432, name="iris", user="iris", password=Secret("x"))


class FakeConnection:
    def execute(self, _query):
        return self


@pytest.fixture
def attempts(monkeypatch):
    monkeypatch.setattr(db.time, "sleep", lambda _s: None)
    calls: list[int] = []

    def install(*outcomes):
        def fake_connect(*_args, **_kwargs):
            outcome = outcomes[min(len(calls), len(outcomes) - 1)]
            calls.append(1)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        monkeypatch.setattr(db, "connect", fake_connect)
        return calls

    return install


def test_database_that_comes_up_later_is_awaited(attempts) -> None:
    starting = psycopg.OperationalError('FATAL:  the database system is starting up')
    refused = psycopg.OperationalError("connection refused")
    calls = attempts(refused, starting, FakeConnection())
    assert isinstance(wait_for_database(SETTINGS, timeout_s=30, application_name="t"), FakeConnection)
    assert len(calls) == 3


@pytest.mark.parametrize("message", [
    'FATAL:  password authentication failed for user "iris"',
    'FATAL:  database "iris" does not exist',
    'FATAL:  role "iris" does not exist',
])
def test_permanent_rejections_fail_fast(attempts, message) -> None:
    calls = attempts(psycopg.OperationalError(f"connection failed: {message}"))
    with pytest.raises(DatabaseUnavailable, match="rejected"):
        wait_for_database(SETTINGS, timeout_s=30, application_name="t")
    assert len(calls) == 1


def test_retries_are_bounded_by_the_timeout(attempts) -> None:
    calls = attempts(psycopg.OperationalError("connection refused"))
    with pytest.raises(DatabaseUnavailable, match="not reachable"):
        wait_for_database(SETTINGS, timeout_s=0.05, application_name="t")
    assert len(calls) >= 1
