"""Create a local, fictional dashboard dataset without calling external services."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed', action='store_true', help='Create data/jobs.db; refuses to overwrite any existing DB')
    args = parser.parse_args()
    source = json.loads((ROOT / 'examples/jobs.json').read_text(encoding='utf-8'))
    if not args.seed:
        print(json.dumps({'mode': 'offline preview', 'synthetic': True, 'jobs': source}, indent=2))
        return
    database = ROOT / 'data/jobs.db'
    # Exclusive creation avoids accidentally replacing a real applicant database.
    with database.open('xb'):
        pass
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(database) as con:
        con.executescript((ROOT / 'data/schema.sql').read_text(encoding='utf-8'))
        for job in source:
            con.execute(
                'INSERT INTO jobs (fingerprint,title,company,location,url,source,date_found,created_at,updated_at,'
                'description,description_available,status,profile_type,final_score,keyword_score,semantic_score,'
                'llm_score,liveness_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (f"demo-{job['id']}", job['title'], job['company'], 'Demo City', 'https://example.invalid/jobs',
                 'synthetic-demo', now, now, now, job['description'], 1, job['status'], job['profile'],
                 job['illustrative_score'], job['illustrative_score'], 0.7, None, 'unknown'),
            )
    print(f'Created {len(source)} fictional jobs in {database.name}. Scores are illustrative, not model results.')


if __name__ == '__main__':
    main()
