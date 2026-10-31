import sqlite3

import pytest

from src import db


@pytest.mark.parametrize('failure', ['inspect', 'alter'])
def test_schema_failure_remains_observable_and_can_be_retried(failure):
    class Connection(sqlite3.Connection):
        fail = True

        def execute(self, sql, args=()):
            matched = sql == 'PRAGMA table_info(jobs)' if failure == 'inspect' else sql.startswith('ALTER TABLE jobs ADD COLUMN raw_html')
            if self.fail and matched:
                self.fail = False
                raise RuntimeError('temporary schema failure')
            return super().execute(sql, args)

    con = sqlite3.connect(':memory:', factory=Connection)
    con.executescript('CREATE TABLE jobs(id INTEGER PRIMARY KEY,status TEXT,final_score REAL);'
                      'CREATE TABLE events(id INTEGER PRIMARY KEY,job_id INTEGER);')
    try:
        with pytest.raises(RuntimeError, match='Could not'):
            db.ensure_v2_schema(con)
        assert db.ensure_v2_schema(con)
        assert 'raw_html' in {row[1] for row in con.execute('PRAGMA table_info(jobs)')}
    finally:
        con.close()
