"""CLI script to execute CareerOS read-only recommendation quality audit."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Add project root to sys.path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.evaluation.quality_audit import run_quality_audit  # noqa: E402


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run automated read-only quality audit comparing saved recommendations against the shared gate."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Path to SQLite database file. Forces provider to SQLite (read-only mode=ro).",
    )
    parser.add_argument(
        "--provider",
        choices=["auto", "sqlite", "turso"],
        default="auto",
        help="Database provider (default: auto; uses Turso if credentials exist, otherwise local SQLite).",
    )
    parser.add_argument(
        "--sample",
        type=Path,
        default=None,
        help="Path to frozen review sample directory (default: output/human-review-20261008 if present).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional path to write JSON audit report (defaults to stdout).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum number of latest evaluated/ready/rejected jobs to sample from DB (default: 100).",
    )
    parser.add_argument(
        "--ids",
        type=str,
        default=None,
        help="Optional comma-separated list of job IDs to audit (e.g. '4291,4515,4624').",
    )
    parser.add_argument(
        "--probe-live",
        action="store_true",
        default=False,
        help="Perform bounded safe liveness probes on explicitly selected sample. Never persists to DB.",
    )
    return parser.parse_args(args)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not 1 <= args.limit <= 500:
        print("[Quality Audit] limit must be between 1 and 500", file=sys.stderr)
        return 2

    id_list: list[str] | None = None
    if args.ids:
        id_list = [i.strip() for i in args.ids.split(",") if i.strip()]

    # If explicit --db is given, provider must be SQLite
    provider = "sqlite" if args.db is not None else args.provider

    if id_list and len(id_list) > 500:
        print("[Quality Audit] at most 500 IDs are allowed", file=sys.stderr)
        return 2
    try:
        report = run_quality_audit(
            db_path=args.db,
            provider=provider,
            sample_dir=args.sample,
            limit=args.limit,
            ids=id_list,
            probe_live=args.probe_live,
        )
    except Exception as exc:
        # Remote errors can contain credentials; do not echo their payloads.
        print(f"[Quality Audit] failed: {type(exc).__name__}", file=sys.stderr)
        return 1

    formatted_json = json.dumps(report, ensure_ascii=False, indent=2)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(formatted_json + "\n", encoding="utf-8")
        print(f"[Quality Audit] Saved report to {args.output}")
    else:
        print(formatted_json)

    return 0


if __name__ == "__main__":
    sys.exit(main())
