# Publication validation

[English](VALIDATION.md) | [Türkçe](VALIDATION.tr.md)

## SQLite-only update — 1 November 2026

The database backend now uses SQLite only. Remote client code, configuration and
the client dependency were removed. Automatic backups keep the latest successful
snapshot; if a new backup fails, the previous one remains. Manual snapshots are
preserved.

For this sanitized publication update, 41 focused database, backup/restore,
transaction, review, audit, pipeline and cache tests passed. Ruff passed for
`src`, `scripts` and `tests`; the offline JSON demo passed; Gitleaks reported no
findings. The complete regression suite was not rerun for this update.

A follow-up fix retries expired negative enrichment-cache entries instead of
reusing an overdue retry date indefinitely. Future retries and terminal misses
remain cached. Its regression cases and related HTTP security and pipeline
checks passed: 28 tests on the sanitized publication copy.

Dashboard reads now use the existing private SQLite snapshot store rather than
recomputing every eligibility decision during login and navigation. Release
preparation warms the active snapshot before activation. Background refresh,
maximum data age, profile/rule invalidation and status changes remain covered.
Twenty-nine dashboard, cache, login and authentication tests passed on this
sanitized copy; targeted Ruff passed. No hosted database dependency was added.

## Earlier validation

Validated on 9 October 2026 (Europe/Istanbul), using the sanitized snapshot:

- Offline regression suite: 411 passed, 7 skipped, 7 subtests passed. Six warnings concern Pydantic's deprecated `.dict()` API.
- Ruff: passed for `src`, `tests` and `scripts/demo.py`.
- Mypy: passed for `src/final_ranking.py`.
- Standard-library JSON demo and exclusive local SQLite seeding: passed.
- Fresh virtual environment: `pip install -r requirements-demo.txt`, demo seeding and loading three dashboard rows passed without the full development dependencies.
- Local Streamlit login, job list and selected-job view: checked with three fictional vacancies. Screenshot: `demo-dashboard.png`.
- Gitleaks v8.30.1: no findings in the sanitized source. Default rules were used without the private history's commit exception.

The original repository history contains a JWT finding and historical applicant documents. Neither is part of this copy. A scanner result does not prove an old credential was revoked. The original remote connection was removed at the owner's request.

The initial Windows sandbox runs failed to create pytest temporary directories. The final run used an isolated copy in the user's temporary directory; test fixtures disabled network access. No live pipeline or paid model evaluation was triggered.

The six synthetic benchmark records were retained; private production examples and their four corresponding dataset-contract tests were removed. The retained institution labels and applicant profile are fictional. No production recommendation-accuracy claim follows from these results.
