# Publication validation

[English](VALIDATION.md) | [Türkçe](VALIDATION.tr.md)

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
