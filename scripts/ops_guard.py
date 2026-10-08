"""Bounded Linux operations check; notify the operator only when health changes."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import requests
from dotenv import load_dotenv


def pipeline_issues(runtime: Path, now: float) -> list[str]:
    reports = sorted(runtime.glob("pipeline_*.json"))
    if not reports:
        return ["pipeline:missing_report"]
    try:
        report = json.loads(reports[-1].read_text(encoding="utf-8"))
        finished = datetime.fromisoformat(report["finished_at"]).timestamp()
        issues = []
        if not 0 <= now - finished <= 26 * 3600:
            issues.append("pipeline:stale_report")
        if report.get("status") != "passed":
            issues.append("pipeline:" + str(report.get("status", "unknown")))
        return issues
    except (ValueError, KeyError, TypeError, OSError):
        return ["pipeline:invalid_report"]


def collect_issues(root: Path, now: float) -> list[str]:
    issues = pipeline_issues(root / "data/runtime", now)
    for service in ("careeros.service", "careeros-web.service"):
        result = subprocess.run(["systemctl", "is-active", "--quiet", service], timeout=15, check=False)
        if result.returncode:
            issues.append("service:" + service)
    try:
        response = requests.get("http://127.0.0.1:8501/_stcore/health", timeout=15)
        if response.status_code != 200 or response.text.strip() != "ok":
            issues.append("dashboard:unhealthy")
    except requests.RequestException:
        issues.append("dashboard:unreachable")
    backups = [p for p in (root / "data/backups").glob("daily-auto-*.json") if p.stat().st_size > 0]
    if not backups or not 0 <= now - max(p.stat().st_mtime for p in backups) <= 50 * 3600:
        issues.append("backup:missing_or_stale")
    return sorted(issues)


def send_notice(message: str) -> None:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise RuntimeError("Telegram configuration missing")
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": message, "disable_web_page_preview": True}, timeout=30,
    )
    response.raise_for_status()
    if response.json().get("ok") is not True:
        raise RuntimeError("Telegram delivery rejected")


def publish_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".ops-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def run(root: Path, *, test_notification: bool = False) -> int:
    load_dotenv(root / ".env")
    now = datetime.now(timezone.utc)
    issues = collect_issues(root, now.timestamp())
    state_path = root / "data/runtime/ops-guard.json"
    try:
        previous = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    changed = issues != previous.get("notified_issues", [])
    if test_notification or changed:
        if issues:
            message = "CareerOS: kontrol gereken durumlar\n" + "\n".join(issues)
        elif test_notification:
            message = "CareerOS kapanış kontrolü: servisler, dashboard, günlük çalışma ve yedek kontrolü başarılı. Hata bildirimi çalışıyor."
        else:
            message = "CareerOS: önceki operasyon sorunu giderildi; kontroller başarılı."
        send_notice(message)
    publish_state(state_path, {"checked_at": now.isoformat(), "issues": issues, "notified_issues": issues})
    print(json.dumps({"issues": issues, "notification_sent": test_notification or changed}))
    return 1 if issues else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--test-notification", action="store_true")
    args = parser.parse_args()
    try:
        return run(args.root.resolve(), test_notification=args.test_notification)
    except Exception as exc:
        # HTTP exceptions can include the bot token in their URL.
        print(json.dumps({"ops_guard_failed": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
