"""Server-private dashboard snapshots; refresh remote data off the UI thread."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time

import pandas as pd

from src import dashboard_data, db


class SnapshotUnavailable(RuntimeError):
    pass


def snapshot_signature():
    from src.eligibility.profile_adapter import compute_profile_version, get_candidate_profile_facts
    from src.eligibility.gate import GATE_RULE_VERSION
    from src.eligibility.experience import EXPERIENCE_RULE_VERSION
    from src.eligibility.language import LANGUAGE_RULE_VERSION
    from src.ops.liveness import CACHE_DURATION_HOURS, LIVENESS_CLASSIFIER_VERSION

    values = [1, os.getenv("TURSO_DB_URL") or db.TURSO_URL,
              compute_profile_version(get_candidate_profile_facts()), GATE_RULE_VERSION,
              EXPERIENCE_RULE_VERSION, LANGUAGE_RULE_VERSION, CACHE_DURATION_HOURS,
              LIVENESS_CLASSIFIER_VERSION]
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


class SnapshotStore:
    def __init__(self, path, signature, fetch, *, max_age=120, refresh_after=30, start=True):
        self.path, self.signature, self.fetch = Path(path), signature, fetch
        self.max_age, self.refresh_after = max_age, refresh_after
        self.lock = threading.RLock()
        self.frame, self.generated_at, self.revision = None, 0, 0
        self.stopped = threading.Event()
        self.wakeup = threading.Event()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload["signature"] == signature:
                self.frame = pd.DataFrame(payload["data"], columns=payload["columns"])
                self.generated_at = float(payload["generated_at"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if start:
            threading.Thread(target=self._loop, daemon=True, name="dashboard-refresh").start()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(self.frame.to_json(orient="split", date_format="iso"))
        data.update(signature=self.signature, generated_at=self.generated_at)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".dashboard-", delete=False) as handle:
                temporary = handle.name
                os.chmod(temporary, 0o600)
                json.dump(data, handle, ensure_ascii=False)
            os.replace(temporary, self.path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def refresh(self):
        with self.lock:
            revision = self.revision
        frame = self.fetch()
        with self.lock:
            # A user transition made during this query must win over its old result.
            if revision != self.revision:
                return
            self.frame, self.generated_at = frame, time.time()
            self._save()

    def _loop(self):
        while not self.stopped.is_set():
            delay = max(0, self.refresh_after - (time.time() - self.generated_at))
            self.wakeup.wait(delay)
            self.wakeup.clear()
            if self.stopped.is_set():
                return
            try:
                self.refresh()
            except Exception:
                # Retain last good data only within max_age; never silently use it forever.
                if self.stopped.wait(self.refresh_after):
                    return

    def invalidate(self):
        with self.lock:
            self.generated_at = 0
            self.revision += 1
        self.wakeup.set()

    def read(self):
        with self.lock:
            age = time.time() - self.generated_at
            if self.frame is None or not 0 <= age <= self.max_age:
                raise SnapshotUnavailable("Güncel ilan verisi hazırlanıyor. Birazdan yeniden yükleyebilirsin.")
            frame = self.frame.copy(deep=True)
        return frame

    def patch_status(self, job_id, status):
        with self.lock:
            self.revision += 1
            if self.frame is None:
                return
            mask = self.frame["id"] == job_id
            self.frame.loc[mask, "status"] = status
            self.frame.loc[mask, "is_ready_recommendation"] = (
                status == "ready_for_review"
            ) & self.frame.loc[mask, "can_recommend"].fillna(False).astype(bool)
            self._save()


_stores = {}
_stores_lock = threading.Lock()


def get_store(include_rejected=False, *, start=True):
    signature = snapshot_signature()
    key = (signature, include_rejected)
    with _stores_lock:
        if key not in _stores:
            # Stop workers tied to a previous candidate profile or ruleset.
            for old_key, store in list(_stores.items()):
                if old_key[0] != signature:
                    store.stopped.set()
                    store.wakeup.set()
                    del _stores[old_key]
            path = db.ROOT / "data" / "cache" / f"dashboard-{signature}-{'archive' if include_rejected else 'active'}.json"
            _stores[key] = SnapshotStore(path, signature, lambda: dashboard_data.fetch_jobs(include_rejected), start=start)
        return _stores[key]


def get_dashboard_jobs(include_rejected=False):
    if db.get_connection_provider() != "Turso":
        return dashboard_data.fetch_jobs(include_rejected)
    frame = get_store(include_rejected).read()
    if not include_rejected and not frame.empty:
        frame = frame[frame["status"].isin(["evaluated", "ready_for_review", "low_priority", "applied", "interview", "offer"])]
    return frame


def start_dashboard_refresh():
    if db.get_connection_provider() == "Turso":
        get_store()


def patch_cached_status(job_id, status):
    with _stores_lock:
        for store in _stores.values():
            store.patch_status(job_id, status)


def refresh_dashboard_cache():
    with _stores_lock:
        for store in _stores.values():
            store.invalidate()


if __name__ == "__main__":
    store = get_store(start=False)
    store.refresh()
    print(f"Dashboard snapshot ready: {len(store.read())} rows")
