"""Load selected dashboard functions without importing or rendering Streamlit UI."""

import ast
from datetime import datetime
import functools
import html
from pathlib import Path
import re
import textwrap

import pandas as pd

from src import db
from src.dashboard_data import JOB_COLUMNS, fetch_jobs
from src.dashboard_cache import get_dashboard_jobs, SnapshotUnavailable
from src.observability.telemetry import safe_log, sanitize_text

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "dashboard.py"


class _StreamlitStub:
    """Only supplies decorators and error reporting used by extracted helpers."""

    @staticmethod
    def cache_data(*args, **kwargs):
        def decorate(function):
            return function

        return decorate

    @staticmethod
    def error(_message):
        raise AssertionError("Dashboard data helper reported a DB error")


def load_dashboard_helpers(root=ROOT):
    names = {
        "parse_job_sections",
        "get_job_skills_tags",
        "get_job_evidence_checklist",
        "get_job_timeline_events",
        "get_job_assets_index",
        "load_data",
    }
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    functions = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    found = {node.name for node in functions}
    if found != names:
        raise RuntimeError(f"Dashboard helper source changed; missing functions: {names - found}")

    namespace = {
        "st": _StreamlitStub(),
        "pd": pd,
        "re": re,
        "html": html,
        "textwrap": textwrap,
        "datetime": datetime,
        "functools": functools,
        "Path": Path,
        "ROOT": Path(root),
        "get_connection": db.get_connection,
        "transition": db.transition,
        "fetch_jobs": fetch_jobs,
        "get_dashboard_jobs": get_dashboard_jobs,
        "SnapshotUnavailable": SnapshotUnavailable,
        "JOB_COLUMNS": JOB_COLUMNS,
        "safe_log": safe_log,
        "sanitize_text": sanitize_text,
    }
    module = ast.Module(body=functions, type_ignores=[])
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return {name: namespace[name] for name in names}
