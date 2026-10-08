from src.db import get_connection
from src.llm.client import canonical_json_bytes, make_evaluation_cache_key, request_with_retry
import json
import os
import requests
import time
from datetime import datetime, timezone
from pathlib import Path
from src.observability.langfuse_client import trace_llm_judge
from src.observability.telemetry import (
    ModelPricingRegistry,
    record_usage,
    safe_log,
    sanitize_text,
)

ROOT = Path(__file__).resolve().parents[1]
LLM_MODEL = os.getenv("LLM_MODEL", "claude-haiku-4-5-20251001")
FIREWORKS_ENDPOINT = "https://api.fireworks.ai/inference/v1/chat/completions"
DEFAULT_FIREWORKS_MODEL = "accounts/fireworks/models/deepseek-v4p1-flash"


def _sanitize_exception(exc):
    """Remove configured credentials before an exception can reach callers."""
    message = sanitize_text(str(exc))
    if isinstance(exc, requests.exceptions.RequestException):
        try:
            return type(exc)(message)
        except Exception:
            return requests.exceptions.RequestException(message)
    return RuntimeError(message)


def ensure_llm_result_storage(con):
    columns = {row[1] for row in con.execute("PRAGMA table_info(jobs)").fetchall()}
    if "llm_judge_result_json" not in columns:
        con.execute("ALTER TABLE jobs ADD COLUMN llm_judge_result_json TEXT")
        con.commit()
    if "llm_model" not in columns:
        try:
            con.execute("ALTER TABLE jobs ADD COLUMN llm_model TEXT")
            con.commit()
        except Exception:  # noqa: S110
            # Column might already exist in concurrent environments
            pass
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_evaluation_cache (
            cache_key TEXT PRIMARY KEY,
            result_json TEXT NOT NULL,
            model TEXT NOT NULL,
            usage_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    con.commit()

# LLM retry/cache policy & Rate-Limit Constants
LLM_MAX_ATTEMPTS = 3
LLM_RETRY_DELAYS_DAYS = (2, 7)
RATE_LIMIT_DELAY_SECONDS = 4.0
GLOBAL_COOLDOWN_SECONDS = 8.0
RATE_LIMIT_RETRY_SECONDS = 60.0
LAST_429_TIMESTAMP = 0.0
JUDGE_RESULT_SCHEMA_VERSION = "judge-result-v1"
_CACHE_VOLATILE_JOB_FIELDS = {
    # Job IDs and ingestion/provenance fields do not change the judge's decision.
    # Exclude them so identical role inputs can share a validated saved result.
    "id", "fingerprint", "date_found", "date_posted", "source", "url",
    "source_query", "source_message_id", "source_query_status", "description_source",
    "created_at", "enriched_at", "enrichment_status", "enrichment_notes",
    "enrichment_attempts", "enrichment_next_attempt_at", "enrichment_terminal",
    "enrichment_cache_key", "knockout_reason", "is_active", "retry_count",
    "liveness_status", "liveness_checked_at", "liveness_http_code", "liveness_detail",
    "status", "llm_judge_status", "llm_judge_attempts", "llm_judge_next_retry_at",
    "llm_judge_terminal", "llm_score", "llm_model", "llm_judge_result_json",
    "updated_at", "last_evaluated", "final_score", "review_priority",
    "last_error", "pipeline_status", "application_materials_path", "metrics_json",
    "version_metadata", "review_notes",
}

USER_PROFILE = {
    "graduation_year": 2027,
    "current_level": "undergraduate student",
    "academic_availability": (
        "The first semester has no scheduled classes. "
        "In the second semester, only one course remains, and there is no attendance requirement "
        "for that course; the user expects to attend only required exams. "
        "Therefore, the user may be available for full-time employment during this period, "
        "subject to the employer's exact schedule and the user's exam dates."
    ),
    "preferred_locations": [
        "Istanbul", "Turkey", "Germany", "Netherlands",
        "Belgium", "Austria", "Switzerland", "Other EU"
    ],
    "target_profiles": ["Wet Lab", "Bioinformatics", "Drug Design"],
    "human_review_required": True,
}

SYSTEM_INSTRUCTIONS = """
You are a strict job-fit judge for a Molecular Biology & Genetics undergraduate student
graduating in 2027.

Use ONLY:
1) the supplied job text,
2) the supplied verified profile facts,
3) the supplied user constraints.

Never invent skills, experience, degrees, dates, sponsorship status, or eligibility.
Do not treat a preferred qualification as required.
Do not reject merely because a job mentions PhD if an alternative degree path is explicitly accepted.

IMPORTANT ACADEMIC AVAILABILITY RULE:
The user is currently an undergraduate student, but the user states that:
- the first semester has no scheduled classes;
- in the second semester, only one course remains;
- that course has no attendance requirement;
- the user expects to attend only required exams.
Therefore, DO NOT infer that undergraduate enrollment automatically prevents full-time employment.
Only mark "full-time availability" as a missing critical requirement when the job text
contains a schedule/availability condition that clearly conflicts with the supplied academic availability.

IMPORTANT RELOCATION RULE:
"relocation_supported" is informational only.
Do not infer willingness or unwillingness to relocate.
Do not use relocation_supported=false as a rejection reason unless the job explicitly requires
a relocation/eligibility condition that the supplied profile cannot satisfy.

Return ONLY valid JSON with exactly:
{
  "is_match": true,
  "profile_type": "Wet Lab|Bioinformatics|Drug Design|Other",
  "match_score": 0,
  "missing_critical_skills": [],
  "relocation_supported": false,
  "confidence": 0.0,
  "reasoning": ""
}

Scoring:
90-100 = excellent fit with no critical gaps
80-89  = strong fit
70-79  = plausible fit but meaningful gaps
50-69  = weak/partial fit
0-49   = poor fit

A hard requirement the user clearly cannot satisfy should normally produce is_match=false.
Do not label a requirement "missing" if the supplied profile actually satisfies it.

TECHNICAL FIT VS. ELIGIBILITY BARRIERS:
Do not treat every degree or experience gap as an equally severe technical mismatch.
First assess technical/domain fit. Then distinguish hard eligibility requirements from preferred/competitive requirements:
- A mandatory degree required for legal/admission eligibility (e.g., Master's strictly required for PhD enrollment) justifies a strong penalty and is_match=false.
- A preferred degree, moderate experience gap, language preference, or industry-experience preference should reduce confidence without erasing strong technical alignment.
- If technical/domain fit is strong (e.g. hands-on qPCR, molecular biology methods, NGS, or transcriptomics pipelines align well) but a non-hard barrier exists, keep the technical match meaningfully reflected in match_score (do not arbitrarily collapse it) and explain the barrier in missing_critical_skills.

DEGREE FIELD EQUIVALENCE & TECHNICAL DOMAIN FIT:
- When a job lists a requirement such as "Degree in Bioinformatics, Computational Biology, or related field" (or "Degree in Bioinformatics" for computational biology positions), evaluate whether the candidate's degree in Molecular Biology & Genetics provides equivalent foundation:
  * If the candidate has verified hands-on coursework, projects, or thesis work covering the exact computational skills requested (e.g. TCGA analysis, differential expression, survival analysis, R/Python pipelines, molecular docking), treat this as an EQUIVALENT FIELD MATCH rather than a hard degree disqualification.
  * Do NOT assign is_match=false solely because the formal degree title says "Molecular Biology & Genetics" instead of "Bioinformatics" when the computational domain competency is verified.
  * Keep strong technical fit scores (match_score >= 75) when the required pipelines and tools are satisfied.
  * CRITICAL DISTINCTION (Degree equivalence is NOT a skill or license waiver):
    - This equivalence rule applies ONLY to related life sciences/computational degree fields at the same degree level.
    - It does NOT waive legally required higher-level degrees (e.g., a mandatory M.Sc. required for PhD enrollment, or an MD/PharmD/Veterinary license).
    - It does NOT waive unverified technical skills. If a job explicitly requires a specific skill (e.g., single-cell RNA-seq, wet-lab assay, clinical trial protocol) that is NOT documented in the verified profile, it MUST remain a genuine missing critical skill and impact scoring accordingly.

LANGUAGE ELIGIBILITY BARRIER RULE:
If a job explicitly specifies that French, German, or any other specific language is MANDATORY or strictly required for daily employment/operations, and the candidate's verified profile shows no documented proficiency in that language:
- Treat this as an explicit eligibility barrier independent of technical qualifications.
- Cap the match_score at 55 maximum (match_score <= 55).
- Do not confuse a language "preference" or "nice to have" with a mandatory requirement.

NON-SCIENCE / COMMERCIAL ROLE CLASSIFICATION:
If the job function is primarily sales, marketing, business development, commercial operations, or account management, and the actual day-to-day role is NOT conducting wet-lab experiments, bioinformatics pipelines, or computational drug design:
- You MUST classify profile_type as "Other".
- The candidate's background in biology/genetics does NOT make a commercial or sales job a "Wet Lab" position.
""".strip()

def sanitize_profile_for_llm(profile_data):
    """Deep-copy and redact candidate personal contact identifiers before LLM ingestion."""
    if not isinstance(profile_data, dict):
        return profile_data
    sanitized = json.loads(json.dumps(profile_data))
    if "identity" in sanitized and isinstance(sanitized["identity"], dict):
        ident = sanitized["identity"]
        ident["email"] = "[REDACTED_CANDIDATE_EMAIL]"
        ident["phone"] = "[REDACTED_PHONE_NUMBER]"
        if "location" in ident:
            ident["location"] = "Candidate Location: Turkey / Willing to relocate"
    return sanitized

def load_master_profile():
    p = ROOT / "data" / "master_cv.json"
    if p.exists():
        raw = json.loads(p.read_text(encoding="utf-8"))
        return sanitize_profile_for_llm(raw)
    return {}

def prune_job_text(text, max_chars=4500):
    if not text:
        return ""
    boilerplate_markers = [
        "Equal Opportunity Employer", "EEO is the Law", "Diversity, Equity",
        "Wir bieten:", "What we offer:", "Benefits:", "Unser Angebot:",
        "Schwerbehinderte Menschen werden", "Datenschutzhinweise",
        "Please note that we do not accept agency"
    ]
    lines = text.splitlines()
    filtered = []
    skip = False
    for line in lines:
        if any(m.lower() in line.lower() for m in boilerplate_markers):
            skip = True
            continue
        if skip and any(kw in line.lower() for kw in ["aufgaben", "responsibilities", "requirements", "profil", "qualifications", "deine aufgaben"]):
            skip = False
        if not skip:
            filtered.append(line)
    pruned = "\n".join(filtered).strip()
    if len(pruned) > max_chars:
        pruned = pruned[:max_chars] + "... [truncated for brevity]"
    return pruned or text[:max_chars]

def build_prompt(job):
    profile = load_master_profile()
    return _build_prompt_with_profile(job, profile)


def _build_prompt_with_profile(job, profile):
    pruned_job = dict(job)
    if "description" in pruned_job:
        pruned_job["description"] = prune_job_text(pruned_job["description"])
    return f"""{SYSTEM_INSTRUCTIONS}

USER CONSTRAINTS:
{json.dumps(USER_PROFILE, ensure_ascii=False, indent=2)}

VERIFIED MASTER PROFILE:
{json.dumps(profile, ensure_ascii=False, indent=2)}

<job_posting>
{json.dumps(pruned_job, ensure_ascii=False, indent=2)}
</job_posting>
"""


def _resolve_provider_model(provider=None, model=None):
    selected = (provider or os.getenv("LLM_PROVIDER", "")).strip().lower()
    if not selected:
        gemini_key = os.getenv("GEMINI_API_KEY")
        anthropic_key = os.getenv("ANTHROPIC_API_KEY")
        if gemini_key and gemini_key != "your_gemini_api_key_here":
            selected = "gemini"
        elif anthropic_key and anthropic_key != "your_anthropic_api_key_here":
            selected = "anthropic"
        else:
            raise RuntimeError("Neither GEMINI_API_KEY nor ANTHROPIC_API_KEY is properly set in .env")

    if selected in ("google",):
        selected = "gemini"
    elif selected in ("claude",):
        selected = "anthropic"
    if selected not in {"gemini", "anthropic", "fireworks"}:
        raise ValueError(f"Unsupported LLM provider: {selected}")

    configured_model = model or os.getenv("LLM_MODEL")
    if selected == "anthropic":
        model_name = configured_model or "claude-haiku-4-5-20251001"
        if "claude" not in model_name.lower():
            model_name = "claude-haiku-4-5-20251001"
    elif selected == "gemini":
        model_name = configured_model or "gemini-2.5-flash"
        if any(token in model_name.lower() for token in ("fireworks", "deepseek", "claude")):
            model_name = "gemini-2.5-flash"
        if model_name.startswith("models/"):
            model_name = model_name[7:]
    else:
        model_name = configured_model or DEFAULT_FIREWORKS_MODEL
        if "claude" in model_name.lower() or "gemini" in model_name.lower():
            model_name = DEFAULT_FIREWORKS_MODEL
    return selected, model_name

def _call_anthropic(prompt, model=None, max_retries=2, base_backoff=3):
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key or key == "your_anthropic_api_key_here":
        raise RuntimeError("ANTHROPIC_API_KEY is not set in .env")

    url = "https://api.anthropic.com/v1/messages"
    model_name = model or os.getenv("LLM_MODEL", "claude-haiku-4-5-20251001")
    if "claude" not in model_name.lower():
        model_name = "claude-haiku-4-5-20251001"

    def log_retry(attempt, attempts, status, error):
        if status == 429:
            print(f"    [{model_name}] 429 Rate Limit hit (attempt {attempt}/{attempts})...")
        elif error:
            print(f"    [{model_name}] Network error (attempt {attempt}/{attempts}): {error}")

    response = request_with_retry(
        url,
        post=requests.post,
        sleep=time.sleep,
        request_kwargs={
            "headers": {
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            "json": {
                "model": model_name,
                "max_tokens": 4000,
                "system": "You are an expert HR recruiter. Always return valid JSON. Do not return any markdown formatting or extra text. Just the raw JSON object.",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
            },
            "timeout": 30,
        },
        max_attempts=max_retries,
        base_backoff=base_backoff,
        retry_statuses={429},
        on_retry=log_retry,
    )
    try:
        response.raise_for_status()
        data = response.json()
        text = data["content"][0]["text"].strip()
        if text.startswith("```json"):
            text = text[7:]
        elif text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        usage = data.get("usage", {})
        return json.loads(text.strip()), model_name, usage
    except Exception as exc:
        print(f"    [{model_name}] Request failed: {exc}")
        raise

def parse_fireworks_judge_response(data):
    choices = data.get("choices", [])
    if not choices:
        raise ValueError("Fireworks response missing choices")
    content = choices[0].get("message", {}).get("content", "")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Fireworks response missing message content")

    text = content.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    if "{" in text and "}" in text:
        text = text[text.find("{"):text.rfind("}") + 1]
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("Fireworks judge response must be a JSON object")

    usage_data = data.get("usage")
    usage = {}
    if isinstance(usage_data, dict) and "prompt_tokens" in usage_data and "completion_tokens" in usage_data:
        usage = {
            "input_tokens": int(usage_data["prompt_tokens"]),
            "output_tokens": int(usage_data["completion_tokens"]),
        }
    return parsed, usage


def call_fireworks(prompt, model=None, max_retries=3, base_backoff=2, timeout=60, return_usage=False):
    """Call Fireworks DeepSeek V4.1 Flash with bounded transient-error retries."""
    key = os.getenv("FIREWORKS_API_KEY", "").strip()
    if not key or key == "your_fireworks_api_key_here":
        raise RuntimeError("FIREWORKS_API_KEY is not set in .env")

    model_name = model or os.getenv("LLM_MODEL") or DEFAULT_FIREWORKS_MODEL
    if "claude" in model_name.lower() or "gemini" in model_name.lower():
        model_name = DEFAULT_FIREWORKS_MODEL
    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4000,
        "temperature": 0.1,
    }
    retry_count = max(1, int(max_retries))
    started = time.time()

    def log_retry(attempt, attempts, status, error):
        if status is not None:
            safe_log(f"[Fireworks:{model_name}] transient HTTP {status}, retry {attempt}/{attempts}")
        elif error:
            safe_log(f"[Fireworks:{model_name}] network error, retry {attempt}/{attempts}: {sanitize_text(str(error))}")

    response = request_with_retry(
        FIREWORKS_ENDPOINT,
        post=requests.post,
        sleep=time.sleep,
        request_kwargs={
            "headers": {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            "json": payload,
            "timeout": timeout,
        },
        max_attempts=retry_count,
        base_backoff=base_backoff,
        retry_statuses={408, 429, *range(500, 600)},
        on_retry=log_retry,
    )
    try:
        response.raise_for_status()
        result, usage = parse_fireworks_judge_response(response.json())
        if usage:
            record_usage(
                provider="fireworks",
                model=model_name,
                prompt_tokens=usage["input_tokens"],
                completion_tokens=usage["output_tokens"],
                latency_seconds=time.time() - started,
                caller="llm_judge",
            )
        if return_usage:
            return result, model_name, usage
        return result, model_name
    except requests.exceptions.HTTPError as exc:
        status = getattr(response, "status_code", None)
        if status not in (408, 429) and not (isinstance(status, int) and 500 <= status < 600):
            safe_log(f"[Fireworks:{model_name}] HTTP error: {sanitize_text(str(exc))}")
        safe_log(f"[Fireworks:{model_name}] request failed: {sanitize_text(str(exc))}")
        raise
    except Exception as exc:
        safe_log(f"[Fireworks:{model_name}] response error: {sanitize_text(str(exc))}")
        raise


def call_gemini(prompt, model=None, max_retries=2, base_backoff=3):
    key = os.getenv("GEMINI_API_KEY")
    if not key or key == "your_gemini_api_key_here":
        raise RuntimeError("GEMINI_API_KEY is not set in .env")

    model_name = model or os.getenv("LLM_MODEL", "gemini-2.5-flash")
    if "fireworks" in model_name.lower() or "deepseek" in model_name.lower() or "claude" in model_name.lower():
        model_name = "gemini-2.5-flash"
    if model_name.startswith("models/"):
        model_name = model_name[7:]

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"

    def log_retry(attempt, attempts, status, error):
        if status in (429, 503):
            print(f"    [{model_name}] HTTP {status} Rate Limit / Overload (attempt {attempt}/{attempts})...")
        elif error:
            print(f"    [{model_name}] Network error (attempt {attempt}/{attempts}): {sanitize_text(str(error))}")

    try:
        response = request_with_retry(
            url,
            post=requests.post,
            sleep=time.sleep,
            request_kwargs={
                "headers": {"Content-Type": "application/json", "x-goog-api-key": key},
                "json": {
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "temperature": 0.1,
                        "responseMimeType": "application/json",
                    },
                },
                "timeout": 45,
            },
            max_attempts=max_retries,
            base_backoff=base_backoff,
            retry_statuses={429, 503},
            on_retry=log_retry,
        )
    except Exception as exc:
        safe_exc = _sanitize_exception(exc)
        safe_log(f"[{model_name}] Request failed: {safe_exc}")
        raise safe_exc from None
    try:
        response.raise_for_status()
        data = response.json()
        candidates = data.get("candidates", [])
        if not candidates:
            raise ValueError(f"Gemini returned no candidates: {data}")
        parts = candidates[0].get("content", {}).get("parts", [])
        if not parts:
            raise ValueError(f"Gemini returned empty parts: {data}")
        text = parts[0].get("text", "").strip()
        if text.startswith("```json"):
            text = text[7:]
        elif text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        usage_meta = data.get("usageMetadata", {})
        usage = {
            "input_tokens": usage_meta.get("promptTokenCount", 0),
            "output_tokens": usage_meta.get("candidatesTokenCount", 0),
        }
        return json.loads(text.strip()), model_name, usage
    except Exception as exc:
        safe_exc = _sanitize_exception(exc)
        safe_log(f"[{model_name}] Request failed: {safe_exc}")
        raise safe_exc from None

def call_claude(prompt, max_retries=2, base_backoff=3, provider=None, model=None, **kwargs):
    """Dispatch to the explicitly configured provider, preserving legacy key-based routing."""
    selected, resolved_model = _resolve_provider_model(provider=provider, model=model)
    if selected == "fireworks":
        return call_fireworks(
            prompt, model=resolved_model, max_retries=max_retries, base_backoff=base_backoff,
            return_usage=True, **kwargs
        )
    if selected == "gemini":
        return call_gemini(prompt, model=resolved_model, max_retries=max_retries, base_backoff=base_backoff)
    return _call_anthropic(prompt, model=resolved_model, max_retries=max_retries, base_backoff=base_backoff)


call_llm = call_claude

def validate_result(result):
    required = {
        "is_match", "profile_type", "match_score",
        "missing_critical_skills", "relocation_supported",
        "confidence", "reasoning",
    }
    missing = required - result.keys()
    if missing:
        raise ValueError(f"LLM JSON missing fields: {sorted(missing)}")
    if result["profile_type"] not in {
        "Wet Lab", "Bioinformatics", "Drug Design", "Other"
    }:
        raise ValueError("Invalid profile_type")
    if not 0 <= float(result["match_score"]) <= 100:
        raise ValueError("Invalid match_score")
    if not 0 <= float(result["confidence"]) <= 1:
        raise ValueError("Invalid confidence")
    if not isinstance(result["missing_critical_skills"], list):
        raise ValueError("missing_critical_skills must be a list")
    return result

def _next_retry(attempts):
    if attempts <= 0:
        return None
    if attempts <= len(LLM_RETRY_DELAYS_DAYS):
        days = LLM_RETRY_DELAYS_DAYS[attempts - 1]
        return (datetime.now(timezone.utc).timestamp() + days * 86400)
    return None


def _iso_from_timestamp(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _load_state(con, job_id):
    row = con.execute(
        """
        SELECT COALESCE(llm_judge_status,'not_attempted'),
               COALESCE(llm_judge_attempts,0),
               llm_judge_next_retry_at,
               COALESCE(llm_judge_terminal,0)
        FROM jobs WHERE id=?
        """,
        (job_id,),
    ).fetchone()
    if not row:
        return "not_attempted", 0, None, 0
    return row


def _should_attempt(status, attempts, next_retry_at, terminal):
    if terminal:
        return False
    if status == "success":
        return False
    if not next_retry_at:
        return True
    try:
        return datetime.fromisoformat(next_retry_at.replace("Z", "+00:00")) <= datetime.now(timezone.utc)
    except Exception:
        return True

def flush_failed_backlog(con=None):
    """
    Resets failed retry timers and attempts for reviewable/evaluated jobs
    so they can be evaluated immediately with the active LLM provider.
    """
    own_con = False
    if con is None:
        con = get_connection()
        own_con = True
    try:
        cur = con.execute("""
            UPDATE jobs
            SET llm_judge_status = 'not_attempted',
                llm_judge_attempts = 0,
                llm_judge_next_retry_at = NULL,
                llm_judge_terminal = 0
            WHERE status IN ('evaluated', 'ready_for_review')
              AND (llm_judge_status = 'failed' OR llm_score IS NULL)
        """)
        if hasattr(con, "commit"):
            con.commit()
        count = cur.rowcount if hasattr(cur, "rowcount") else 0
        print(f"[LLM Backlog Flush] Reset {count} pending failed jobs for immediate evaluation.")
        return count
    finally:
        if own_con:
            con.close()

def run(limit=25):
    global LAST_429_TIMESTAMP
    con = get_connection()
    con.execute("PRAGMA foreign_keys=ON")
    ensure_llm_result_storage(con)

    selected = con.execute("""
        SELECT *, COALESCE(keyword_score,match_score,0) AS _runtime_keyword_score
        FROM jobs
        WHERE status IN ('evaluated', 'ready_for_review')
          AND description_available=1
          AND profile_type IN ('Bioinformatics', 'Wet Lab', 'Drug Design')
          AND (
            llm_judge_status IS NULL
            OR llm_judge_status = 'not_attempted'
            OR (llm_judge_status IN ('rate_limited', 'failed')
                AND COALESCE(llm_judge_terminal, 0) = 0
                AND (llm_judge_next_retry_at IS NULL
                     OR julianday(llm_judge_next_retry_at) IS NULL
                     OR julianday(llm_judge_next_retry_at) <= julianday('now')))
          )
          AND semantic_score IS NOT NULL
        ORDER BY match_score DESC
        LIMIT ?
    """, (limit,))
    column_names = [description[0] for description in selected.description]
    rows = selected.fetchall()

    results = []
    processed = 0
    skipped = 0
    consecutive_429_count = 0

    for row in rows:
        job_fields = dict(zip(column_names, row, strict=True))
        job_id = job_fields["id"]
        title = job_fields.get("title")
        company = job_fields.get("company")
        location = job_fields.get("location")
        description = job_fields.get("description")
        requirements = job_fields.get("requirements_text")
        education = job_fields.get("education_requirements")
        experience = job_fields.get("experience_requirements")
        eligibility = job_fields.get("eligibility_text")
        profile = job_fields.get("profile_type")
        semantic_score = job_fields.get("semantic_score")
        keyword_score = job_fields.get("_runtime_keyword_score")

        status, attempts, next_retry_at, terminal = _load_state(con, job_id)

        if not _should_attempt(status, attempts, next_retry_at, terminal):
            skipped += 1
            continue

        job = {
            "title": title,
            "company": company,
            "location": location,
            "description": description,
            "requirements": requirements,
            "education_requirements": education,
            "experience_requirements": experience,
            "eligibility_text": eligibility,
            "preclassified_profile": profile,
            "semantic_score": semantic_score,
            "keyword_score": keyword_score,
        }

        new_attempts = attempts + 1
        processed += 1
        stamp = datetime.now(timezone.utc).isoformat()

        raw_res = None
        model_used: str | None = None
        usage = {"input_tokens": 0, "output_tokens": 0}

        runtime_profile = load_master_profile()
        runtime_profile_bytes = canonical_json_bytes(runtime_profile)
        prompt = _build_prompt_with_profile(job, runtime_profile)
        try:
            provider_used, requested_model = _resolve_provider_model()
        except RuntimeError:
            # Retain a deterministic cache identity even when an injected adapter
            # is used by an offline caller without provider credentials.
            configured_provider = os.getenv("LLM_PROVIDER") or "anthropic"
            provider_used, requested_model = _resolve_provider_model(provider=configured_provider)
        cache_job_fields = {
            key: value for key, value in job_fields.items()
            if key not in _CACHE_VOLATILE_JOB_FIELDS and key != "_runtime_keyword_score"
        }
        cache_key = make_evaluation_cache_key(
            job_fields={**cache_job_fields, "effective_keyword_score": keyword_score},
            runtime_profile_bytes=runtime_profile_bytes,
            prompt=prompt,
            schema_version=JUDGE_RESULT_SCHEMA_VERSION,
            model=requested_model,
            provider=provider_used,
        )
        cached = con.execute(
            "SELECT result_json, model, usage_json FROM llm_evaluation_cache WHERE cache_key=?",
            (cache_key,),
        ).fetchone()
        if cached:
            try:
                raw_res = json.loads(cached[0])
                validate_result(raw_res)
                model_used = cached[1]
                # A reused result incurs no new provider tokens or cost in this run.
                usage = {"input_tokens": 0, "output_tokens": 0}
                print(f"    [LLM Cache] Reused exact evaluation for job #{job_id}")
            except (TypeError, ValueError, json.JSONDecodeError):
                con.execute("DELETE FROM llm_evaluation_cache WHERE cache_key=?", (cache_key,))
                raw_res = None

        if raw_res is None:
            # Check global cooldown if a 429 was recently encountered
            elapsed_since_429 = time.time() - LAST_429_TIMESTAMP
            if elapsed_since_429 < GLOBAL_COOLDOWN_SECONDS:
                remaining_cooldown = GLOBAL_COOLDOWN_SECONDS - elapsed_since_429
                print(f"    [Global Cooldown] Cooling down for {remaining_cooldown:.1f}s before next candidate...")
                time.sleep(remaining_cooldown)

            # Standard polite throttle between requests
            time.sleep(RATE_LIMIT_DELAY_SECONDS)

        t_start = time.perf_counter()
        try:
            if raw_res is None:
                raw_res, model_used, usage = call_claude(prompt)
            if not isinstance(model_used, str) or not model_used.strip():
                raise ValueError("LLM provider returned no model identifier")
            latency_ms = round((time.perf_counter() - t_start) * 1000, 1)
            inp_tok = usage.get("input_tokens", 0)
            out_tok = usage.get("output_tokens", 0)
            cost_usd = ModelPricingRegistry.calculate_cost(model_used, inp_tok, out_tok)

            result = validate_result(raw_res)
            cached_result_json = json.dumps(result, ensure_ascii=False)
            result["job_id"] = job_id
            result["evaluated_at"] = stamp
            result["model_used"] = model_used
            results.append(result)
            consecutive_429_count = 0

            # Langfuse Observability Trace (Safely isolated, zero privacy leakage)
            trace_llm_judge(
                job_id=job_id,
                model=model_used,
                latency_ms=latency_ms,
                input_tokens=inp_tok,
                output_tokens=out_tok,
                cost_usd=cost_usd,
                success=True,
                match_score=float(result.get("match_score", 0)),
                is_match=bool(result.get("is_match", False)),
                profile_type=result.get("profile_type"),
                error=None
            )

            con.execute(
                """
                UPDATE jobs
                SET llm_judge_status='success',
                    llm_judge_attempts=?,
                    llm_judge_next_retry_at=NULL,
                    llm_judge_terminal=0,
                    llm_score=?,
                    llm_model=?,
                    llm_judge_result_json=?,
                    updated_at=?
                WHERE id=?
                """,
                (new_attempts, float(result["match_score"]), model_used, json.dumps(result, ensure_ascii=False), stamp, job_id),
            )
            con.execute(
                """
                INSERT OR REPLACE INTO llm_evaluation_cache
                    (cache_key, result_json, model, usage_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (cache_key, cached_result_json, model_used, json.dumps(usage, ensure_ascii=False), stamp),
            )

        except Exception as exc:
            latency_ms = round((time.perf_counter() - t_start) * 1000, 1)
            # Langfuse Observability Trace for Failure
            trace_llm_judge(
                job_id=job_id,
                model=LLM_MODEL,
                latency_ms=latency_ms,
                input_tokens=0,
                output_tokens=0,
                cost_usd=0.0,
                success=False,
                error=str(exc)
            )
            is_429 = "429" in str(exc)
            if is_429:
                LAST_429_TIMESTAMP = time.time()
                next_retry = _iso_from_timestamp(LAST_429_TIMESTAMP + max(
                    RATE_LIMIT_RETRY_SECONDS,
                    GLOBAL_COOLDOWN_SECONDS,
                ))
                consecutive_429_count += 1
                print(f"    [Rate Limit Notice] Job #{job_id} encountered rate limit. Preserving candidate state for next batch.")
                results.append({"job_id": job_id, "error": str(exc), "rate_limited": True})
                con.execute(
                    """
                    UPDATE jobs
                    SET llm_judge_status='rate_limited',
                        llm_judge_attempts=?,
                        llm_judge_next_retry_at=?,
                        updated_at=?
                    WHERE id=?
                    """,
                    (new_attempts, next_retry, stamp, job_id),
                )
                if consecutive_429_count >= 3:
                    print("    [Circuit Breaker] Google API quota saturated. Pausing LLM judge so pipeline can finalize.")
                    con.commit()
                    break
            else:
                consecutive_429_count = 0
                terminal_now = new_attempts >= LLM_MAX_ATTEMPTS
                next_retry = None if terminal_now else _iso_from_timestamp(
                    _next_retry(new_attempts)
                )

                results.append({"job_id": job_id, "error": str(exc)})
                con.execute(
                    """
                    UPDATE jobs
                    SET llm_judge_status='failed',
                        llm_judge_attempts=?,
                        llm_judge_next_retry_at=?,
                        llm_judge_terminal=?,
                        updated_at=?
                    WHERE id=?
                    """,
                    (new_attempts, next_retry, int(terminal_now), stamp, job_id),
                )

        con.commit()

    con.close()

    print(f"LLM JUDGE: model={LLM_MODEL}")
    print(
        f"LLM JUDGE: processed={processed} | skipped={skipped} | "
        f"eligible_rows={len(rows)}"
    )
    print("Results saved: Database (jobs.llm_judge_result_json)")


if __name__ == "__main__":
    import sys
    limit_arg = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    run(limit=limit_arg)
