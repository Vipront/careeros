# -*- coding: utf-8 -*-
"""
Manual Job Ingestion Service for CareerOS.
Provides safe validation, SSRF protection, deduplication, and pipeline integration
for user-provided jobs via Telegram (/ekle) and Streamlit Dashboard.
"""
import urllib.parse
from dataclasses import dataclass
from typing import Optional, Tuple, Dict, Any

import requests
from bs4 import BeautifulSoup

import src.db
from src.ops import safe_http
from src.db import now, transition
from src.collectors.linkedin_crawler import make_fingerprint
from src.filters.knockout import knockout_reason
from src.matcher.keyword_matcher import verified_skill_matches, choose_profile, relevance_score
from src.final_ranking import calculate_final_score

MAX_TEXT_LENGTH = 50000
FETCH_TIMEOUT = 12
MAX_RESPONSE_SIZE = 2 * 1024 * 1024 # 2 MB

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

@dataclass
class ManualIngestionResult:
    success: bool
    job_id: Optional[int] = None
    status: Optional[str] = None
    final_score: Optional[float] = None
    is_duplicate: bool = False
    reason: Optional[str] = None
    error: Optional[str] = None

def normalize_url(url: str) -> str:
    """Remove known tracking parameters while preserving posting identity."""
    p = urllib.parse.urlparse(url.strip())
    query = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
    query = [(key, value) for key, value in query
             if not key.lower().startswith("utm_")
             and key.lower() not in {"gclid", "fbclid", "msclkid"}]
    return urllib.parse.urlunparse((
        p.scheme.lower(), p.netloc.lower(), p.path if p.params else p.path.rstrip("/"), p.params,
        urllib.parse.urlencode(sorted(query)), "",
    ))

def is_private_ip(hostname: str) -> bool:
    """Compatibility helper that fails closed for unresolved or mixed DNS."""
    url_host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    valid, _ = safe_http.validate_public_http_url(f"https://{url_host}/")
    return not valid

def validate_url(url: str) -> Tuple[bool, Optional[str]]:
    """Validates URL format, scheme, credentials, and all resolved addresses."""
    return safe_http.validate_public_http_url(url)

def sanitize_input_text(text: str) -> Tuple[bool, str, Optional[str]]:
    """Validates user-submitted job text bounds."""
    if not text or not str(text).strip():
        return False, "", "İlan metni boş olamaz."
    cleaned = str(text).strip()
    if len(cleaned) > MAX_TEXT_LENGTH:
        return False, "", f"İlan metni azami uzunluğu aşıyor ({len(cleaned)} > {MAX_TEXT_LENGTH})."
    return True, cleaned, None

def fetch_job_from_url(url: str) -> Tuple[bool, str, Optional[str]]:
    """Safely fetches HTML content from web with limits."""
    resp = None
    try:
        resp = safe_http.safe_get(
            url,
            headers=HEADERS,
            timeout=FETCH_TIMEOUT,
        )

        if resp.status_code != 200:
            return False, "", f"Sayfa yüklenemedi (HTTP {resp.status_code})."

        content_type = resp.headers.get("Content-Type", "").lower()
        if "text/html" not in content_type and "text/plain" not in content_type:
            return False, "", f"Desteklenmeyen içerik türü: {content_type}"

        content_bytes = bytearray()
        for chunk in safe_http.iter_content(resp, chunk_size=8192):
            content_bytes.extend(chunk)
            if len(content_bytes) > MAX_RESPONSE_SIZE:
                return False, "", "Sayfa boyutu 2MB sınırını aşıyor."

        html = content_bytes.decode(resp.encoding or "utf-8", errors="replace")
        soup = BeautifulSoup(html, "html.parser")

        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()

        text = soup.get_text(separator="\n")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        extracted_text = "\n".join(lines)
        return True, extracted_text[:MAX_TEXT_LENGTH], None

    except safe_http.SafeHTTPError as exc:
        return False, "", f"Güvenli URL isteği engellendi: {exc}"
    except requests.exceptions.Timeout:
        return False, "", "URL isteği zaman aşımına uğradı."
    except Exception as exc:
        return False, "", f"İçerik çekilemedi: {exc}"
    finally:
        if resp is not None:
            resp.close()

def run_job_pipeline_stages(job_id: int, con) -> Dict[str, Any]:
    """
    Executes standard pipeline stages synchronously for a single job:
    Knockout -> Keyword -> Semantic/Ranking
    """
    row = con.execute("SELECT title, company, location, description, requirements_text FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return {"status": "error", "reason": "Job not found"}

    title, company, location, desc, req_text = row
    full_text = f"{title or ''} {company or ''} {location or ''} {desc or ''} {req_text or ''}"

    # 1. Knockout stage
    reason = knockout_reason(title or "", full_text)
    if reason:
        transition(job_id, "rejected", note=f"Manual Ingestion Knockout: {reason}")
        con.execute("UPDATE jobs SET knockout_reason = ?, updated_at = ? WHERE id = ?", (reason, now(), job_id))
        con.commit()
        return {"status": "rejected", "reason": reason, "final_score": 0.0}

    # Transition to evaluated
    transition(job_id, "evaluated", note="Manual Ingestion Passed Knockout")

    # 2. Keyword matching
    matches = verified_skill_matches(full_text)
    profile, _ = choose_profile(full_text)
    kw_score = float(relevance_score(full_text, matches, profile))

    # 3. Final score calculation (No heavy semantic model load during fast web/bot ingestion)
    final_score = calculate_final_score(kw_score, None, None)

    con.execute(
        """
        UPDATE jobs
        SET keyword_score = ?,
            match_score = ?,
            final_score = ?,
            profile_type = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (kw_score, kw_score, final_score, profile, now(), job_id)
    )
    con.commit()

    # 4. Shared recommendation gate check
    from src.eligibility.gate import evaluate_job_eligibility, persist_eligibility_decision
    from src.eligibility.models import OverallEligibilityStatus

    gate = evaluate_job_eligibility({
        "id": job_id,
        "title": title or "",
        "company": company or "",
        "location": location or "",
        "description": desc or "",
        "requirements_text": req_text or "",
    })
    persist_eligibility_decision(con, job_id, gate)
    if gate.overall_status == OverallEligibilityStatus.INELIGIBLE:
        reason = "; ".join(gate.hard_block_reasons)
        transition(job_id, "rejected", note=f"Manual Ingestion Ineligible: {reason}")
        con.execute("UPDATE jobs SET knockout_reason = ?, updated_at = ? WHERE id = ?", (reason, now(), job_id))
        con.commit()
        return {"status": "rejected", "reason": reason, "final_score": 0.0}

    # This score is a fast keyword-only preview. Semantic matching and the LLM
    # judge consume evaluated jobs, so keep every non-knockout job eligible for
    # those stages regardless of its provisional score.
    pending_reason = (
        f"Provisional keyword score {final_score:.1f}; pending semantic and LLM evaluation."
    )
    return {"status": "evaluated", "final_score": final_score, "reason": pending_reason}

def process_manual_job(
    url: Optional[str] = None,
    text: Optional[str] = None,
    title: Optional[str] = None,
    company: Optional[str] = None,
    location: Optional[str] = None,
    description: Optional[str] = None,
    connection=None
) -> ManualIngestionResult:
    """
    Unified entry point for manual job creation from Telegram or Dashboard.
    """
    raw_url = (url or "").strip()
    canonical_url = normalize_url(raw_url) if raw_url else ""

    con = connection or src.db.get_connection()
    close_con = connection is None

    try:
        # 1. Check duplicate by URL
        if canonical_url:
            # Stored URLs predate normalization. Compare the same canonical form
            # on both sides instead of treating a shared path prefix as identity.
            existing_url = next((
                row[:3] for row in con.execute(
                    "SELECT id, status, final_score, url FROM jobs WHERE url IS NOT NULL AND url != ''"
                ).fetchall() if normalize_url(row[3]) == canonical_url
            ), None)
            if existing_url:
                jid, status, score = existing_url
                return ManualIngestionResult(
                    success=False,
                    job_id=jid,
                    status=status,
                    final_score=score,
                    is_duplicate=True,
                    reason=f"Bu ilan daha önce sisteme kaydedilmiş (İlan #{jid}, Durum: {status})."
                )

        # 2. Resolve content
        job_desc = (description or text or "").strip()
        if not job_desc and raw_url:
            ok, fetched_text, err = fetch_job_from_url(raw_url)
            if not ok:
                return ManualIngestionResult(success=False, error=f"İlan URL'i çekilemedi: {err}")
            job_desc = fetched_text

        text_ok, job_desc, text_error = sanitize_input_text(job_desc)
        if not text_ok:
            return ManualIngestionResult(success=False, error=text_error)

        job_title = (title or "").strip()
        if not job_title:
            first_line = job_desc.splitlines()[0][:100].strip()
            job_title = first_line if first_line else "Manual Job Position"

        job_company = (company or "Direct Application").strip()
        job_location = (location or "Unspecified").strip()

        # 3. Check duplicate by fingerprint
        fp = make_fingerprint(job_title, job_company, job_location)
        existing_fp = con.execute("SELECT id, status, final_score FROM jobs WHERE fingerprint = ?", (fp,)).fetchone()
        if existing_fp:
            jid, status, score = existing_fp
            return ManualIngestionResult(
                success=False,
                job_id=jid,
                status=status,
                final_score=score,
                is_duplicate=True,
                reason=f"Bu ilan başlık/şirket parmak iziyle zaten mevcut (İlan #{jid})."
            )

        stamp = now()
        cur = con.execute(
            """
            INSERT INTO jobs (
                fingerprint, title, company, location, url, source,
                date_found, created_at, updated_at, description,
                status, is_active, description_available
            )
            VALUES (?, ?, ?, ?, ?, 'manual', ?, ?, ?, ?, 'new', 1, 1)
            """,
            (fp, job_title, job_company, job_location, raw_url or canonical_url, stamp, stamp, stamp, job_desc)
        )
        job_id = cur.lastrowid
        con.commit()

        # 4. Trigger pipeline stages
        pipeline_res = run_job_pipeline_stages(job_id, con)
        status_row = con.execute("SELECT status, final_score FROM jobs WHERE id = ?", (job_id,)).fetchone()
        cur_status = status_row[0] if status_row else pipeline_res.get("status")
        cur_score = status_row[1] if status_row else pipeline_res.get("final_score")

        return ManualIngestionResult(
            success=True,
            job_id=job_id,
            status=cur_status,
            final_score=cur_score,
            reason=pipeline_res.get("reason")
        )

    except Exception as exc:
        return ManualIngestionResult(success=False, error=f"İşleme hatası: {exc}")
    finally:
        if close_con:
            con.close()
