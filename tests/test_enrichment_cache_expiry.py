import pytest

import run_daily
from src import db
from src.enrichment import dynamic_enricher as enrichment
from tests.db_helpers import isolated_database, seed_job


@pytest.mark.parametrize('retry,terminal,expected_calls', [
    ('2000-01-01T00:00:00+00:00', 0, 1),
    ('2099-01-01T00:00:00+00:00', 0, 0),
    ('2000-01-01T00:00:00+00:00', 1, 0),
])
def test_negative_cache_retries_only_when_due(monkeypatch, retry, terminal, expected_calls):
    calls = []

    def search(*args):
        calls.append(args)
        return [], {}

    monkeypatch.setattr(enrichment, 'tavily_search_candidates', search)
    monkeypatch.setattr(enrichment, 'serper_search_candidates', lambda *args: ([], {}))
    monkeypatch.setattr(enrichment.time, 'sleep', lambda _: None)
    with isolated_database() as path:
        job_id = seed_job(path, fingerprint='expired-cache', title='Synthetic role', company='Synthetic', status='evaluated')
        con = db.get_connection(path)
        key = enrichment.enrichment_cache_key('Synthetic role', 'Synthetic', 'Test City')
        con.execute("UPDATE jobs SET description_available=0,enrichment_status='not_attempted',enrichment_attempts=1 WHERE id=?", (job_id,))
        con.execute("INSERT INTO enrichment_cache(cache_key,status,attempts,fetched_at,next_retry_at,terminal) VALUES(?,'not_found',1,?,?,?)",
                    (key, '2000-01-01T00:00:00+00:00', retry, terminal))
        con.commit()
        con.close()
        enrichment.run(limit=1)
        assert len(calls) == expected_calls
        assert run_daily.get_pending_count() == 0
        con = db.get_connection(path)
        try:
            assert con.execute('SELECT enrichment_attempts FROM jobs WHERE id=?', (job_id,)).fetchone()[0] == 1 + expected_calls
        finally:
            con.close()
