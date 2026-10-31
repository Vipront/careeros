import sqlite3

import pytest

from src.db import transition


@pytest.fixture
def connection():
    con = sqlite3.connect(':memory:')
    con.executescript('CREATE TABLE jobs(id INTEGER PRIMARY KEY, status TEXT, updated_at TEXT);'
                      'CREATE TABLE events(job_id INTEGER, event_type TEXT, event_time TEXT, note TEXT);'
                      "INSERT INTO jobs VALUES(1,'ready_for_review',NULL);")
    yield con
    con.close()


def test_transition_commits_status_and_matching_event(connection):
    transition(1, 'applied', note='quality check', connection=connection)
    assert connection.execute('SELECT status FROM jobs').fetchone() == ('applied',)
    assert connection.execute('SELECT job_id,event_type,note FROM events').fetchone() == (
        1, 'status_change', 'ready_for_review -> applied: quality check')
    assert not connection.in_transaction


def test_invalid_transition_changes_nothing(connection):
    with pytest.raises(ValueError, match='Invalid transition'):
        transition(1, 'offer', connection=connection)
    assert connection.execute('SELECT status FROM jobs').fetchone() == ('ready_for_review',)
    assert connection.execute('SELECT count(*) FROM events').fetchone() == (0,)


def test_audit_failure_rolls_back_status(connection):
    connection.execute("CREATE TRIGGER reject_event BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'audit unavailable'); END")
    with pytest.raises(sqlite3.IntegrityError, match='audit unavailable'):
        transition(1, 'applied', connection=connection)
    assert connection.execute('SELECT status FROM jobs').fetchone() == ('ready_for_review',)
    assert connection.execute('SELECT count(*) FROM events').fetchone() == (0,)


def test_losing_concurrent_transition_creates_no_event(connection):
    class ConcurrentConnection:
        def execute(self, sql, args=()):
            if sql.startswith('UPDATE jobs SET status='):
                connection.execute("UPDATE jobs SET status='withdrawn' WHERE id=1")
                connection.commit()
            return connection.execute(sql, args)

        def rollback(self):
            connection.rollback()

    with pytest.raises(ValueError, match='concurrently'):
        transition(1, 'applied', connection=ConcurrentConnection())
    assert connection.execute('SELECT status FROM jobs').fetchone() == ('withdrawn',)
    assert connection.execute('SELECT count(*) FROM events').fetchone() == (0,)
