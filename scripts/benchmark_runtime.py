"""Measure real server reads and profile encoding; no pipeline or provider calls."""
import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("root", type=Path)
parser.add_argument("output", type=Path)
args = parser.parse_args()
root = args.root.resolve()
sys.path.insert(0, str(root))
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
from src import db  # noqa: E402
import run_daily  # noqa: E402
from src.matcher import semantic  # noqa: E402

def connection():
    if db.get_connection_provider() == "Turso":
        return db.TursoConnection(db.TURSO_URL, db.TURSO_TOKEN)
    import sqlite3
    return sqlite3.connect((root / "data/jobs.db").as_uri() + "?mode=ro", uri=True)

run_daily._open_queue_connection = connection
observations = []
for _ in range(3):
    started = time.perf_counter()
    count = run_daily.get_pending_count()
    observations.append(time.perf_counter() - started)
report = {"scope": "real server, read-only queue and semantic profile component; not full pipeline",
          "pending_count": count, "queue_seconds": observations,
          "queue_median_seconds": statistics.median(observations)}
started = time.perf_counter()
model = semantic.load_model()
report["model_load_seconds"] = time.perf_counter() - started
profile_bytes = (root / "data/semantic_profiles.json").read_bytes()
profiles = json.loads(profile_bytes)
with tempfile.TemporaryDirectory(prefix="careeros-benchmark-") as temp:
    started = time.perf_counter()
    fresh = semantic._load_or_encode_profile_vectors(model, profiles, profile_bytes, temp)
    report["profile_cache_miss_seconds"] = time.perf_counter() - started
    hits = []
    for _ in range(3):
        started = time.perf_counter()
        cached = semantic._load_or_encode_profile_vectors(model, profiles, profile_bytes, temp)
        hits.append(time.perf_counter() - started)
    import numpy as np
    report["cached_vectors_equal"] = bool(np.array_equal(fresh, cached))
report["profile_cache_hit_seconds"] = hits
report["profile_cache_hit_median_seconds"] = statistics.median(hits)
report["profile_component_speedup"] = report["profile_cache_miss_seconds"] / statistics.median(hits)
args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
print(json.dumps(report))
