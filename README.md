# CareerOS

[English](README.md) | [Türkçe](README.tr.md)

An AI-assisted career workflow for molecular biology, bioinformatics and computational drug discovery: collect vacancies, check eligibility, rank opportunities and prepare application drafts for human review.

This copy uses fictional applicant data and includes an offline demo. Its new Git history excludes the original database, applicant documents, credentials and operational records. The interface is primarily Turkish.

![CareerOS data flow](docs/architecture.svg)

![Local dashboard with three fictional vacancies](docs/demo-dashboard.png)

The screenshot uses only fictional data. Its displayed scores are illustrative.

## What it does

- Combines keyword matching, sentence embeddings and structured LLM evaluation.
- Checks experience, education and language requirements, and tracks uncertain evidence.
- Uses SQLite locally, with an optional Turso/LibSQL backend.
- Tracks review and application states with an audit trail.
- Presents a Streamlit dashboard and optional Telegram notifications.
- Produces CV and cover-letter drafts with verification checks before review.

It is decision support: a high match score is not a guarantee of eligibility, an interview or an application submission.

## Try it without credentials

Use Python 3.12 or newer. This first command uses only the standard library, makes no network requests and does not create a database:

```sh
python scripts/demo.py
```

The three fictional vacancies and their illustrative scores are in [examples/jobs.json](examples/jobs.json). The scores are supplied demo values, not model predictions.

To explore the dashboard locally:

```sh
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements-demo.txt
python scripts/demo.py --seed
```

Set a local password, then launch the dashboard. In PowerShell:

```powershell
$env:DASHBOARD_PASSWORD = 'choose-your-local-password'
python -m streamlit run src/dashboard.py --server.address 127.0.0.1
```

In macOS/Linux shells:

```sh
export DASHBOARD_PASSWORD='choose-your-local-password'
python -m streamlit run src/dashboard.py --server.address 127.0.0.1
```

Open `http://localhost:8501`. Do not set Turso credentials for this demo. Seeding refuses to overwrite an existing database. The offline preview and local dashboard do not run the live pipeline.

## Full pipeline setup

```sh
python -m pip install -r requirements.txt
# Windows: Copy-Item .env.example .env
# macOS/Linux: cp .env.example .env
```

Configure the integrations you need in `.env`, replace the fictional profile in `data/master_cv.json` and `data/master_cv.md`, and review the domain-specific document templates before use. Install Playwright Chromium with `python -m playwright install chromium` if using browser collection or PDF rendering. Sentence-transformer models require an initial download. Live search, enrichment and LLM evaluation may incur provider charges.

Only after configuration and review, run `python run_daily.py`. Telegram is optional; `python -m src.telegram_bot` starts its operator interface. This public snapshot does not include private deployment addresses or an existing applicant database.

## Architecture

`collectors → enrichment → eligibility → keyword/semantic matching → LLM judge → final ranking → document verification → human review`

The database is the source of truth; application state changes use `src/db.py`. Source code is under `src/`, offline regression tests under `tests/`, and the fictional fixtures under `data/` and `examples/`.

## Limits and evaluation

Matching remains sensitive to incomplete job descriptions, stale vacancies, language requirements and experience interpretation. Synthetic regression checks do not establish real-world recommendation accuracy. Private production benchmarks and human evaluation records are excluded from this snapshot. Some generation templates retain domain-specific example education and research wording; they require review before use with another applicant.

See [validation](docs/VALIDATION.md) for checks actually run on this snapshot. Historical deployment results are not presented as current verification of this public copy.

## Development

```sh
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
python -m ruff check src tests scripts/demo.py
python -m mypy src/final_ranking.py
```

Tests block live network access. The publication copy replaces personal fixtures, so its validation results must be assessed separately from the private deployment.

## Authorship and reuse

Uğur Cem Yıldız developed CareerOS with AI assistance, defining its workflow requirements and acceptance decisions. AI tools helped with implementation and verification.

Source is shared for inspection under [LICENSE](LICENSE); redistribution and reuse require permission. Dependency licenses remain their respective owners' terms. Secrets and applicant data must never be committed; see [SECURITY.md](SECURITY.md).
