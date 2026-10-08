import os
import json
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent
PYTHON = sys.executable
LOCK_PATH = ROOT / "data" / "pipeline.lock"
RUN_REPORT_DIR = ROOT / "data" / "runtime"


class QueueInspectionError(RuntimeError):
    """Raised when queue state cannot be read reliably enough to make a decision."""

def acquire_lock(lock_path=None):
    """
    Acquires an atomic OS-level process file lock.
    Compatible with Windows (msvcrt) and Linux (fcntl).
    Returns file handle if lock acquired, None if already locked by another running process.
    """
    target = lock_path or str(LOCK_PATH)
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    try:
        f = open(target, "r+")
    except FileNotFoundError:
        try:
            f = open(target, "w+")
        except (IOError, OSError):
            return None
    except (IOError, OSError):
        return None

    if os.name == "nt":
        import msvcrt
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except (IOError, OSError, PermissionError):
            f.close()
            return None
    else:
        import fcntl
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (IOError, OSError):
            f.close()
            return None

    try:
        f.seek(0)
        f.truncate()
        f.write(f"{os.getpid()}\n")
        f.flush()
    except Exception as exc:
        print(f"[Pipeline Lock] Could not record PID: {type(exc).__name__}")
    return f

def release_lock(f, lock_path=None):
    """Releases the OS-level process file lock and closes the handle."""
    if not f:
        return
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except Exception as exc:
        print(f"[Pipeline Lock] Release warning: {type(exc).__name__}")
    finally:
        try:
            f.close()
        except Exception as exc:
            print(f"[Pipeline Lock] Close warning: {type(exc).__name__}")

def cleanup_old_outputs(days=30, output_dir=None):
    """
    Retention policy cleaner: Removes YYYY-MM-DD date folders under output/ older than `days` days.
    Safely ignores non-date folders and handles exceptions without crashing the pipeline.
    """
    import shutil
    import stat
    from datetime import timedelta

    target = Path(output_dir) if output_dir else (ROOT / "output")
    try:
        target_stat = target.lstat()
    except FileNotFoundError:
        return 0
    except OSError as exc:
        print(f"[Retention Cleanup] Warning: Could not inspect output directory: {exc}")
        return 0

    def is_reparse(path_stat):
        return bool(
            getattr(path_stat, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        )
    if stat.S_ISLNK(target_stat.st_mode) or is_reparse(target_stat) or not stat.S_ISDIR(target_stat.st_mode):
        print("[Retention Cleanup] Warning: Refusing an output target that is not a regular directory.")
        return 0
    try:
        resolved_target = target.resolve(strict=True)
    except OSError as exc:
        print(f"[Retention Cleanup] Warning: Could not resolve output directory: {exc}")
        return 0

    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    now_dt = datetime.now()
    cutoff_date = (now_dt - timedelta(days=days)).date()
    removed_count = 0

    try:
        for child in list(target.iterdir()):
            if not date_pattern.fullmatch(child.name):
                continue
            try:
                child_stat = child.lstat()
                if stat.S_ISLNK(child_stat.st_mode) or is_reparse(child_stat) or not stat.S_ISDIR(child_stat.st_mode):
                    continue
                resolved_child = child.resolve(strict=True)
                if resolved_child.parent != resolved_target:
                    print(f"[Retention Cleanup] Warning: Refusing path outside output directory: {child}")
                    continue
            except OSError as dir_err:
                print(f"[Retention Cleanup] Warning: Could not inspect {child.name}: {dir_err}")
                continue
            try:
                folder_date = datetime.strptime(child.name, "%Y-%m-%d").date()
                if folder_date < cutoff_date:
                    shutil.rmtree(resolved_child)
                    removed_count += 1
                    print(f"[Retention Cleanup] Removed expired output folder: {child.name}")
            except ValueError:
                continue
            except Exception as dir_err:
                print(f"[Retention Cleanup] Warning: Could not remove {child.name}: {dir_err}")
    except Exception as exc:
        print(f"[Retention Cleanup] Warning: Error during output retention cleanup: {exc}")

    if output_dir is None and removed_count:
        try:
            from src.db import reconcile_application_paths

            reconcile_application_paths(resolved_target)
        except Exception as exc:
            print(f"[Retention Cleanup] Warning: Could not reconcile application paths: {exc}")

    return removed_count

STEPS_CRAWL = [
    ("linkedin_crawler", [PYTHON, "-m", "src.collectors.linkedin_crawler"]),
]

STEPS_PROCESS = [
    ("enrichment", [PYTHON, "-m", "src.enrichment.dynamic_enricher"]),
    ("knockout", [PYTHON, "-m", "src.filters.knockout"]),
    ("keyword", [PYTHON, "-m", "src.matcher.keyword_matcher"]),
    ("semantic", [PYTHON, "-m", "src.matcher.semantic"]),
    ("llm", [PYTHON, "-m", "src.llm_judge"]),
    ("ranking", [PYTHON, "-m", "src.final_ranking"]),
    ("liveness", [PYTHON, "-m", "src.ops.liveness"]),
    ("documents", [PYTHON, "-m", "src.documents.generator"]),
]

STEPS_FINAL = [
    (
        "quality_audit",
        [
            PYTHON,
            str(ROOT / "scripts" / "audit_recommendations.py"),
            "--limit",
            "100",
            "--output",
            str(ROOT / "output" / "quality-audit-latest.json"),
        ],
    ),
    ("telegram", [PYTHON, "-m", "src.telegram_notify"]),
]


def run_step(name, command, failures, timeout=None):
    step_timeout = timeout if timeout is not None else (
        1200 if name in ("linkedin_crawler", "enrichment") else 1800 if name == "semantic" else 600
    )
    process = None
    reader = None
    timed_out = False
    try:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )
        output_queue = queue.Queue()
        end_of_output = object()

        def read_output():
            try:
                for output_line in process.stdout:
                    output_queue.put(output_line)
            except Exception as exc:
                output_queue.put(exc)
            finally:
                output_queue.put(end_of_output)
                try:
                    process.stdout.close()
                except (OSError, ValueError):
                    pass

        reader = threading.Thread(target=read_output, name=f"{name}-stdout", daemon=True)
        reader.start()
        deadline = time.monotonic() + step_timeout
        output_complete = False
        while not output_complete:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                item = output_queue.get(timeout=remaining)
            except queue.Empty:
                timed_out = True
                break
            if item is end_of_output:
                output_complete = True
            elif isinstance(item, Exception):
                raise item
            else:
                sys.stdout.write(item)
                sys.stdout.flush()

        if timed_out:
            raise subprocess.TimeoutExpired(command, step_timeout)
        process.wait(timeout=max(0, deadline - time.monotonic()))
        print(f"[{name}] returncode={process.returncode}")

        if process.returncode != 0:
            failures.append(name)
    except subprocess.TimeoutExpired:
        timed_out = True
        print(f"[{name}] TIMEOUT EXPIRED")
        failures.append(name)
    except Exception as exc:
        failures.append(name)
        print(f"[{name}] EXCEPTION: {exc}")
    finally:
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        if process is not None:
            try:
                process.wait()
            except OSError:
                pass
        if reader is not None:
            reader.join(timeout=1)
        if process is not None and process.stdout is not None and not process.stdout.closed:
            try:
                process.stdout.close()
            except (OSError, ValueError):
                pass

def _open_queue_connection():
    from src.db import get_connection
    return get_connection()


def get_pending_count():
    con = None
    try:
        con = _open_queue_connection()
        from src.db import now

        c1 = con.execute("""
            SELECT COUNT(*) FROM jobs
            WHERE status IN ('new','evaluated')
              AND (description_available=0 OR description_available IS NULL)
              AND (enrichment_status IS NULL OR enrichment_status IN ('not_attempted','not_found'))
              AND COALESCE(enrichment_terminal,0)=0
              AND (
                    enrichment_status IS NULL
                    OR enrichment_status='not_attempted'
                    OR enrichment_next_attempt_at IS NULL
                    OR enrichment_next_attempt_at <= ?
              )
        """, (now(),)).fetchone()[0]
        c2 = con.execute("SELECT COUNT(*) FROM jobs WHERE status='evaluated' AND description_available=1 AND semantic_score IS NULL").fetchone()[0]
        c3 = con.execute("""
            SELECT COUNT(*) FROM jobs
            WHERE status IN ('evaluated', 'ready_for_review') AND description_available=1 AND semantic_score IS NOT NULL
              AND profile_type IN ('Bioinformatics', 'Wet Lab', 'Drug Design')
              AND (
                llm_judge_status IS NULL
                OR llm_judge_status='not_attempted'
                OR (llm_judge_status IN ('rate_limited', 'failed') AND COALESCE(llm_judge_terminal, 0) = 0 AND (llm_judge_next_retry_at IS NULL OR julianday(llm_judge_next_retry_at) IS NULL OR julianday(llm_judge_next_retry_at) <= julianday('now')))
              )
        """).fetchone()[0]
        return c1 + c2 + c3
    except Exception as exc:
        raise QueueInspectionError(f"Could not inspect pending pipeline queue: {exc}") from exc
    finally:
        if con is not None:
            try:
                con.close()
            except Exception as exc:
                raise QueueInspectionError(f"Could not close pending-queue connection: {exc}") from exc


def stage_has_work(stage):
    """Return whether a supported stage has eligible rows, using its production predicate."""
    queries = {
        "semantic": """
            SELECT 1 FROM jobs
            WHERE status='evaluated' AND description_available=1 AND semantic_score IS NULL
            ORDER BY id LIMIT 1
        """,
        "llm": """
            SELECT 1 FROM jobs
            WHERE status IN ('evaluated', 'ready_for_review')
              AND description_available=1
              AND profile_type IN ('Bioinformatics', 'Wet Lab', 'Drug Design')
              AND semantic_score IS NOT NULL
              AND (
                llm_judge_status IS NULL
                OR llm_judge_status='not_attempted'
                OR (llm_judge_status IN ('rate_limited', 'failed')
                    AND COALESCE(llm_judge_terminal, 0)=0
                    AND (llm_judge_next_retry_at IS NULL
                         OR julianday(llm_judge_next_retry_at) IS NULL
                         OR julianday(llm_judge_next_retry_at) <= julianday('now')))
              )
            ORDER BY match_score DESC LIMIT 1
        """,
    }
    if stage not in queries:
        raise ValueError(f"No reliable work predicate is defined for stage: {stage}")
    con = None
    try:
        con = _open_queue_connection()
        return con.execute(queries[stage]).fetchone() is not None
    except Exception as exc:
        raise QueueInspectionError(f"Could not inspect {stage} stage queue: {exc}") from exc
    finally:
        if con is not None:
            try:
                con.close()
            except Exception as exc:
                raise QueueInspectionError(f"Could not close {stage} queue connection: {exc}") from exc


def write_run_report(report, report_path=None):
    """Atomically write a run-scoped JSON report; path is injectable for callers/tests."""
    import tempfile

    target = Path(report_path) if report_path else RUN_REPORT_DIR / f"pipeline_{report['run_id']}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    finally:
        try:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        except OSError:
            pass
    return target

def run(report_path=None):
    started_at = datetime.now().astimezone()
    run_id = started_at.strftime("%Y%m%dT%H%M%S.%f%z")
    report = {
        "schema_version": 1,
        "run_id": run_id,
        "started_at": started_at.isoformat(),
        "finished_at": None,
        "status": "running",
        "exit_code": None,
        "stages": [],
        "pending_count_observations": [],
        "remaining_pending_count": None,
        "stop_reason": None,
        "failures": [],
    }
    lock_file = acquire_lock()
    if not lock_file:
        print("[Pipeline Lock] Another pipeline run is already in progress. Exiting cleanly to avoid concurrency conflicts.")
        return 2

    failures = []
    critical_stages = {"linkedin_crawler", "knockout", "keyword", "semantic", "llm", "ranking", "documents", "telegram", "queue_inspection"}
    exit_code = 0

    def execute_stage(name, command):
        stage_started = time.monotonic()
        before = len(failures)
        event = {"name": name, "started_at": datetime.now().astimezone().isoformat()}
        try:
            if name in {"semantic", "llm"}:
                if not stage_has_work(name):
                    event.update({"result": "skipped", "reason": "production queue predicate found no eligible rows"})
                    return
            run_step(name, command, failures)
            event["result"] = "failed" if len(failures) > before else "passed"
        except QueueInspectionError as exc:
            failures.append("queue_inspection")
            event.update({"result": "failed", "reason": str(exc), "failure_kind": "queue_inspection"})
            raise
        finally:
            event["duration_seconds"] = round(time.monotonic() - stage_started, 6)
            event["finished_at"] = datetime.now().astimezone().isoformat()
            report["stages"].append(event)

    try:
        stamp = datetime.now().isoformat()
        print(f"\n=== RUN {stamp} ===")

        # 1. Run crawler once
        for name, command in STEPS_CRAWL:
            execute_stage(name, command)

        # 2. Loop processing steps until no pending jobs
        loop_count = 1
        MAX_LOOPS = 6
        last_pending = -1
        remaining_pending = None

        while loop_count <= MAX_LOOPS:
            print(f"\n--- STARTING PROCESSING LOOP {loop_count}/{MAX_LOOPS} ---")

            try:
                for name, command in STEPS_PROCESS:
                    execute_stage(name, command)
            except QueueInspectionError:
                print("DAILY PIPELINE: Queue inspection failed; stopping processing without assuming an empty queue.")
                break

            critical_failures = [f for f in failures if f in critical_stages]
            if critical_failures:
                print("DAILY PIPELINE: Critical failures detected, breaking loop.")
                break

            try:
                queue_started = time.monotonic()
                pending = get_pending_count()
                report["stages"].append({
                    "name": "queue_inspection",
                    "result": "passed",
                    "purpose": "pending backlog count",
                    "duration_seconds": round(time.monotonic() - queue_started, 6),
                })
            except QueueInspectionError as exc:
                failures.append("queue_inspection")
                report["stages"].append({
                    "name": "queue_inspection",
                    "result": "failed",
                    "purpose": "pending backlog count",
                    "reason": str(exc),
                    "duration_seconds": round(time.monotonic() - queue_started, 6),
                })
                print(f"DAILY PIPELINE: {exc}")
                break
            report["pending_count_observations"].append({"loop": loop_count, "count": pending})
            print(f"[Loop Controller] Pending backlog items remaining: {pending}")
            remaining_pending = pending

            if pending == 0:
                report["stop_reason"] = "queue_empty"
                print("\n--- NO PENDING JOBS LEFT. FINISHED ALL LOOPS ---")
                break

            if pending == last_pending:
                report["stop_reason"] = "stalled_backlog"
                print("\n[Loop Controller] Backlog count did not decrease (API limits or saturated queue). Finalizing pipeline now.")
                break

            last_pending = pending
            loop_count += 1

        if remaining_pending and loop_count > MAX_LOOPS and report["stop_reason"] is None:
            report["stop_reason"] = "loop_limit"
        report["remaining_pending_count"] = remaining_pending

        # 3. Final steps (Telegram)
        print("\n--- RUNNING FINAL STEPS ---")
        for name, command in STEPS_FINAL:
            execute_stage(name, command)

        critical_failures = [f for f in failures if f in critical_stages]

        if failures:
            print("=== PIPELINE STAGE FAILURES DETECTED ===")
            for f in failures:
                print(f"FAILED STAGE: {f}")

        if critical_failures:
            print("DAILY PIPELINE: Critical failures in:", ", ".join(critical_failures))
            exit_code = 1
        elif remaining_pending:
            print(
                "DAILY PIPELINE: INCOMPLETE; "
                f"{remaining_pending} pending item(s) remain ({report['stop_reason']})."
            )
            exit_code = 3
        else:
            # 4. Housekeeping: Output Retention Policy (clean folders older than 30 days)
            try:
                cleanup_old_outputs(days=30)
            except Exception as c_err:
                print(f"[Retention Cleanup] Non-fatal cleanup warning: {c_err}")

            if failures:
                print("DAILY PIPELINE: PASS WITH NONCRITICAL FAILURES")
            else:
                print("DAILY PIPELINE: PASS")
            exit_code = 0

    except Exception as exc:
        failures.append("pipeline")
        report["fatal_error"] = f"{type(exc).__name__}: {exc}"
        print(f"DAILY PIPELINE: Fatal error: {exc}")
        exit_code = 1
    finally:
        release_lock(lock_file)
        report["finished_at"] = datetime.now().astimezone().isoformat()
        if exit_code:
            report["status"] = "incomplete" if exit_code == 3 else "failed"
        elif failures:
            report["status"] = "passed_with_noncritical_failures"
        else:
            report["status"] = "passed"
        report["exit_code"] = exit_code
        report["failures"] = list(dict.fromkeys(failures))
        try:
            path = write_run_report(report, report_path)
            print(f"[Pipeline Report] {path}")
        except Exception as report_exc:
            print(f"[Pipeline Report] Could not write run report: {report_exc}")
            exit_code = 1
    return exit_code

if __name__ == "__main__":
    exit_code = run()
    sys.exit(exit_code)
