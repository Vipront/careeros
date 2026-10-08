# -*- coding: utf-8 -*-
"""
Job Link Liveness and Expiration Verification Service for Gmailv6.
Periodically checks whether 'ready_for_review' job postings are still reachable,
detects soft/hard expirations, authwalls, and redirects without altering FSM status.
"""
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Dict, Any

import requests

from src.ops import safe_http
from src.db import get_connection, utc_now

import os

CACHE_DURATION_HOURS = float(os.getenv("LIVENESS_TTL_HOURS", "12"))
MAX_RESPONSE_SIZE = 2 * 1024 * 1024 # 2MB
HTTP_TIMEOUT = 12

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,tr;q=0.8,de;q=0.7",
}

# Reliable regex patterns for closed/expired job postings (EN, TR, DE, FR)
CLOSED_PATTERNS = [
    r"\b(job|position|vacancy|posting|role)\s+(is\s+)?(no longer available|closed|expired|filled)\b",
    r"\bthis\s+job\s+is\s+no\s+longer\s+accepting\s+applications\b",
    r"\bthis\s+listing\s+has\s+expired\b",
    r"\bbu\s+ilan\s+(yayından\s+kaldırılmış(tır)?|kapanmıştır|artık\s+aktif\s+değil)\b",
    r"\bbu\s+pozisyon\s+için\s+başvurular\s+kapanmıştır\b",
    r"\bdieses\s+stellenangebot\s+ist\s+(leider\s+)?nicht\s+mehr\s+verfügbar\b",
    r"\bdiese\s+stelle\s+ist\s+bereits\s+besetzt\b",
    r"\bcette\s+offre\s+d'emploi\s+n'est\s+plus\s+disponible\b",
]

LIVENESS_CLASSIFIER_VERSION = "liveness-v2:positive-posting"

# Positive posting requirement: Both substantive job details AND an application control / JobPosting structure.
# Generic careers pages (list of jobs, 'our careers', generic navigation) lack one or both and must return UNKNOWN.
APPLICATION_CONTROL_PATTERNS = [
    r"\b(?:apply\s+(?:now|here|online|for\s+this\s+job|for\s+this\s+role|for\s+position)|submit\s+application|send\s+cv|send\s+resume)\b",
    r"\b(?:easy\s+apply|jetzt\s+bewerben|candidater|postuler|başvur(?:u\s+yap)?)\b",
    r'<form[^>]*\b(?:apply|job|candidature|bewerb)[^>]*>',
    r'href=["\'][^"\']*(?:/apply|/candidature|/bewerbung|/apply-now)[^"\']*',
    r'"(?:directApply|applyUrl)"\s*:',
]

SUBSTANTIVE_JOB_PATTERNS = [
    r"\b(?:job\s+description|stellenangebot|offre\s+d'emploi|iş\s+ilanı)\b",
    r"\b(?:requirements|qualifications|responsibilities|missions|anforderungen|qualifikationen|aufgaben|nitelikler|aranan\s+nitelikler)\b",
    r"\b(?:we\s+are\s+looking\s+for|about\s+the\s+role|about\s+the\s+position|key\s+duties)\b",
    r"\b(?:scientist|technician|fellow|researcher|engineer|assistant|intern|bioinformatician|postdoc)\b",
]

JOB_POSTING_SCHEMA_PATTERNS = [
    r'"@type"\s*:\s*"JobPosting"',
]

POSITIVE_POSTING_PATTERNS = APPLICATION_CONTROL_PATTERNS + SUBSTANTIVE_JOB_PATTERNS

CAPTCHA_PATTERNS = [
    r"\b(?:cf-turnstile|recaptcha|hcaptcha|cloudflare)\b",
    r"\b(?:verify\s+you\s+are\s+a\s+human|verify\s+you\s+are\s+not\s+a\s+robot|robot\s+check)\b",
    r"\b(?:checking\s+your\s+browser|security\s+check|bot\s+detection)\b",
    r"\b(?:please\s+complete\s+the\s+security\s+check|attention\s+required!\s+\|\s+cloudflare)\b",
]


def is_liveness_stale(checked_at_str: Optional[str], ttl_hours: float = CACHE_DURATION_HOURS) -> bool:
    """Return True if positive liveness evidence is missing, in the future, or older than TTL."""
    if not checked_at_str:
        return True
    try:
        dt = datetime.fromisoformat(checked_at_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now_dt = datetime.now(timezone.utc)
        diff_seconds = (now_dt - dt).total_seconds()
        # Future timestamp defect: timestamps > 60s in the future are invalid / stale
        if diff_seconds < -60.0:
            return True
        return diff_seconds > (ttl_hours * 3600.0)
    except (ValueError, TypeError):
        return True


AUTH_URL_PATTERNS = [
    r"/login",
    r"/signin",
    r"/sign-in",
    r"/auth",
    r"/authwall",
    r"/checkpoint",
]


class LivenessStatus(str, Enum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
    NOT_FOUND = "NOT_FOUND"
    REDIRECTED = "REDIRECTED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"
    TEMPORARY_ERROR = "TEMPORARY_ERROR"
    UNKNOWN = "UNKNOWN"
    INVALID_URL = "INVALID_URL"


@dataclass
class LivenessResult:
    status: LivenessStatus
    http_status: Optional[int] = None
    final_url: Optional[str] = None
    detail: Optional[str] = None


def check_liveness(url: str, *, validator=None, requester=None) -> LivenessResult:
    """
    Safely tests whether a job URL is live, reachable, closed, rate limited or redirected.
    Guards against initial SSRF and redirect SSRF targets.
    """
    validate = validator or safe_http.validate_public_http_url
    is_valid, err_msg = validate(url)
    if not is_valid:
        return LivenessResult(status=LivenessStatus.INVALID_URL, detail=err_msg or "Geçersiz URL")

    try:
        resp = safe_http.safe_get(
            url,
            headers=DEFAULT_HEADERS,
            timeout=HTTP_TIMEOUT,
            validator=validate,
            requester=requester,
        )
    except safe_http.SafeHTTPError as exc:
        return LivenessResult(status=LivenessStatus.INVALID_URL, detail=str(exc))
    except requests.Timeout:
        return LivenessResult(
            status=LivenessStatus.TEMPORARY_ERROR,
            detail="Timeout: Connection timed out"
        )
    except requests.RequestException as exc:
        err_name = type(exc).__name__
        return LivenessResult(
            status=LivenessStatus.TEMPORARY_ERROR,
            detail=f"Network error: {err_name}"
        )
    except Exception as exc:
        return LivenessResult(
            status=LivenessStatus.TEMPORARY_ERROR,
            detail=f"Unexpected error: {exc}"
        )

    try:
        return _classify_liveness_response(url, resp)
    finally:
        resp.close()


def _classify_liveness_response(url: str, resp) -> LivenessResult:
    """Classify an already-fetched response; caller owns and closes it."""
    http_status = resp.status_code
    final_url = resp.url or url

    if http_status != 200 and not (200 <= http_status < 300):
        if http_status == 404:
            return LivenessResult(status=LivenessStatus.NOT_FOUND, http_status=404, final_url=final_url)
        if http_status == 410:
            return LivenessResult(status=LivenessStatus.CLOSED, http_status=410, final_url=final_url, detail="HTTP 410 Gone")
        if http_status == 429:
            return LivenessResult(status=LivenessStatus.RATE_LIMITED, http_status=429, final_url=final_url, detail="HTTP 429 Too Many Requests")
        if http_status in (401, 403):
            return LivenessResult(status=LivenessStatus.AUTH_REQUIRED, http_status=http_status, final_url=final_url, detail=f"HTTP {http_status} Access Denied")
        if 400 <= http_status < 500:
            return LivenessResult(status=LivenessStatus.UNKNOWN, http_status=http_status, final_url=final_url, detail=f"HTTP {http_status} Client Error")
        if http_status >= 500:
            return LivenessResult(status=LivenessStatus.TEMPORARY_ERROR, http_status=http_status, final_url=final_url, detail=f"HTTP {http_status} Server Error")
        return LivenessResult(status=LivenessStatus.UNKNOWN, http_status=http_status, final_url=final_url, detail=f"HTTP {http_status} Non-2xx Response")

    parsed_initial = urllib.parse.urlparse(url)
    parsed_final = urllib.parse.urlparse(final_url)
    final_url_lower = final_url.lower()

    if any(re.search(pat, final_url_lower) for pat in AUTH_URL_PATTERNS):
        return LivenessResult(
            status=LivenessStatus.AUTH_REQUIRED,
            http_status=http_status,
            final_url=final_url,
            detail=f"Redirected to authentication portal: {final_url}"
        )

    if parsed_initial.path.strip("/") and not parsed_final.path.strip("/"):
        return LivenessResult(
            status=LivenessStatus.REDIRECTED,
            http_status=http_status,
            final_url=final_url,
            detail="Redirected to homepage"
        )

    final_path = parsed_final.path.rstrip("/").lower()
    initial_path = parsed_initial.path.rstrip("/").lower()
    if initial_path and final_path in ("/careers", "/jobs", "/stellenangebote", "/karriere"):
        if initial_path != final_path:
            return LivenessResult(
                status=LivenessStatus.REDIRECTED,
                http_status=http_status,
                final_url=final_url,
                detail=f"Redirected from specific job to general jobs directory: {final_url}"
            )

    body_bytes = bytearray()
    try:
        for chunk in safe_http.iter_content(resp, chunk_size=65536):
            if chunk:
                remaining = MAX_RESPONSE_SIZE - len(body_bytes)
                body_bytes.extend(chunk[:remaining])
                if len(body_bytes) >= MAX_RESPONSE_SIZE:
                    break
    except requests.Timeout as read_err:
        return LivenessResult(
            status=LivenessStatus.TEMPORARY_ERROR,
            http_status=http_status,
            final_url=final_url,
            detail=f"Timeout reading response: {type(read_err).__name__}",
        )
    except (requests.RequestException, OSError) as read_err:
        print(f"[Liveness] Stream chunk read warning: {read_err}")

    content_type = resp.headers.get("content-type", "").lower()
    if "text" not in content_type and "json" not in content_type:
        return LivenessResult(
            status=LivenessStatus.UNKNOWN,
            http_status=http_status,
            final_url=final_url,
            detail=f"Non-HTML content type: {content_type}"
        )

    try:
        body_text = body_bytes.decode(resp.encoding or "utf-8", errors="replace")
    except Exception:
        body_text = body_bytes.decode("utf-8", errors="replace")

    body_lower = body_text.lower()

    # 7. LinkedIn specific authwall/checkpoint check
    if "linkedin.com" in final_url_lower:
        if "authwall" in final_url_lower or "checkpoint" in final_url_lower or "sign in" in body_lower[:800]:
            return LivenessResult(
                status=LivenessStatus.AUTH_REQUIRED,
                http_status=http_status,
                final_url=final_url,
                detail="LinkedIn guest authwall or login challenge"
            )

    # 8. Content-level Closed / Expired pattern check
    for pat in CLOSED_PATTERNS:
        match = re.search(pat, body_lower)
        if match:
            return LivenessResult(
                status=LivenessStatus.CLOSED,
                http_status=http_status,
                final_url=final_url,
                detail=f"Matched closed pattern: '{match.group(0)}'"
            )

    # 8.5 Bot or Captcha verification challenge check
    for pat in CAPTCHA_PATTERNS:
        match = re.search(pat, body_lower)
        if match:
            return LivenessResult(
                status=LivenessStatus.AUTH_REQUIRED,
                http_status=http_status,
                final_url=final_url,
                detail=f"Captcha / bot verification challenge: '{match.group(0)}'"
            )

    # 9. Verify positive posting evidence before asserting ACTIVE:
    # Requires BOTH substantive job details AND an application control / JobPosting structure.
    # A generic careers directory, list of roles, or generic navigation without both must return UNKNOWN.
    has_substantive = any(re.search(pat, body_lower) for pat in SUBSTANTIVE_JOB_PATTERNS)
    has_control = any(re.search(pat, body_lower) for pat in APPLICATION_CONTROL_PATTERNS)
    has_schema = any(re.search(pat, body_lower) for pat in JOB_POSTING_SCHEMA_PATTERNS)

    has_positive_evidence = (
        (has_substantive and has_control)
        or (has_schema and (has_substantive or has_control))
    )
    if not has_positive_evidence or len(body_text.strip()) < 30:
        return LivenessResult(
            status=LivenessStatus.UNKNOWN,
            http_status=http_status,
            final_url=final_url,
            detail="HTTP 200 returned but positive job posting, substantive job details, and application controls absent",
        )

    # Default to ACTIVE if 200 OK and positive cues found and no closure patterns found
    return LivenessResult(
        status=LivenessStatus.ACTIVE,
        http_status=200,
        final_url=final_url,
        detail=f"{LIVENESS_CLASSIFIER_VERSION} - Page accessible and application open",
    )


def run_liveness_checks(con=None, force: bool = False, job_ids: Optional[list[int]] = None) -> Dict[str, Any]:
    """
    Scans candidate jobs in the database and updates their liveness metadata.
    Covers ready_for_review jobs and evaluated/normal candidate recommendations before documents/notifications.
    Respects CACHE_DURATION_HOURS TTL unless force=True.
    Never raises an exception that stops caller; updates records individually.
    """
    close_con = False
    if con is None:
        con = get_connection()
        close_con = True

    summary = {
        "total_eligible": 0,
        "checked_count": 0,
        "skipped_cached": 0,
        "active_count": 0,
        "closed_count": 0,
        "other_count": 0,
        "errors": 0,
    }

    try:
        columns = {c[1] for c in con.execute("PRAGMA table_info(jobs)").fetchall()}
        score_expr = "COALESCE(final_score, llm_score, 0)" if "llm_score" in columns else "COALESCE(final_score, 0)"

        if job_ids:
            placeholders = ",".join("?" for _ in job_ids)
            rows = con.execute(f"""
                SELECT id, url, liveness_status, liveness_checked_at
                FROM jobs
                WHERE id IN ({placeholders})
                ORDER BY id DESC
            """, tuple(job_ids)).fetchall()
        else:
            rows = con.execute(f"""
                SELECT id, url, liveness_status, liveness_checked_at
                FROM jobs
                WHERE (
                    status = 'ready_for_review'
                    OR (status IN ('evaluated', 'normal') AND {score_expr} >= 50)
                )
                ORDER BY id DESC
            """).fetchall()

        for row in rows:
            jid = row[0]
            url = row[1]
            last_checked = row[3]

            if not url or not str(url).startswith("http"):
                continue

            # Cache verification: skip if fresh within TTL
            if not force and not is_liveness_stale(last_checked, ttl_hours=CACHE_DURATION_HOURS):
                summary["skipped_cached"] += 1
                continue

            summary["total_eligible"] += 1
            summary["checked_count"] += 1

            try:
                result = check_liveness(str(url))
                check_stamp = utc_now()

                con.execute("""
                    UPDATE jobs
                    SET liveness_status = ?,
                        liveness_checked_at = ?,
                        liveness_http_code = ?,
                        liveness_detail = ?
                    WHERE id = ?
                """, (
                    result.status.value,
                    check_stamp,
                    result.http_status,
                    (result.detail or "")[:250],
                    jid
                ))
                if hasattr(con, "commit"):
                    con.commit()

                if result.status == LivenessStatus.ACTIVE:
                    summary["active_count"] += 1
                elif result.status == LivenessStatus.CLOSED:
                    summary["closed_count"] += 1
                else:
                    summary["other_count"] += 1

            except Exception as j_err:
                summary["errors"] += 1
                print(f"[Liveness Worker] Error verifying job #{jid}: {j_err}")

    except Exception as exc:
        print(f"[Liveness Worker] Fatal query error: {exc}")
    finally:
        if close_con:
            con.close()

    return summary


if __name__ == "__main__":
    print("[Liveness Worker] Starting manual scan on ready_for_review jobs...")
    res = run_liveness_checks(force=False)
    print(f"[Liveness Worker] Completed: {res}")
