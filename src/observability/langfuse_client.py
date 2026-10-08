# -*- coding: utf-8 -*-
import os
import uuid
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger("langfuse_client")

_client = None
_init_attempted = False

def is_langfuse_enabled() -> bool:
    """Checks if Langfuse tracing is enabled via environment variable."""
    val = os.getenv("LANGFUSE_ENABLED", "false").strip().lower()
    return val in ("true", "1", "yes", "on")

def get_langfuse_client():
    """Lazily initializes and returns the Langfuse client if enabled and credentials exist."""
    global _client, _init_attempted
    if not is_langfuse_enabled():
        return None
    if _client is not None or _init_attempted:
        return _client

    _init_attempted = True
    try:
        from langfuse import Langfuse
        pk = os.getenv("LANGFUSE_PUBLIC_KEY")
        sk = os.getenv("LANGFUSE_SECRET_KEY")
        host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
        if not pk or not sk:
            logger.info("Langfuse enabled but keys missing. Tracing disabled.")
            return None
        _client = Langfuse(public_key=pk, secret_key=sk, host=host, debug=False)
        return _client
    except Exception as exc:
        logger.warning(f"Failed to initialize Langfuse client: {exc}")
        _client = None
        return None

def trace_llm_judge(
    job_id: int,
    model: str,
    latency_ms: float,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    success: bool,
    run_id: Optional[str] = None,
    match_score: Optional[float] = None,
    is_match: Optional[bool] = None,
    profile_type: Optional[str] = None,
    error: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Records a sanitized LLM judge execution trace in Langfuse.
    PRIVACY GUARANTEE: Never transmits master CV, personal contact details, or candidate identity.
    Fails safely and quietly if Langfuse is unreachable.
    """
    correlation_id = run_id or f"run-{job_id}-{str(uuid.uuid4())[:8]}"
    error_category = None
    if error:
        err_lower = error.lower()
        if "429" in err_lower or "rate" in err_lower:
            error_category = "RATE_LIMIT"
        elif "timeout" in err_lower:
            error_category = "TIMEOUT"
        elif "500" in err_lower or "502" in err_lower or "503" in err_lower:
            error_category = "UPSTREAM_API_ERROR"
        else:
            error_category = "EXECUTION_ERROR"

    trace_data = {
        "job_id": job_id,
        "run_id": correlation_id,
        "model": model,
        "latency_ms": latency_ms,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens
        },
        "cost_usd": cost_usd,
        "success": success,
        "output_summary": {
            "match_score": match_score,
            "is_match": is_match,
            "profile_type": profile_type
        },
        "error": error,
        "error_category": error_category
    }

    client = get_langfuse_client()
    if not client:
        return trace_data

    try:
        # Start sanitized generation observation (Langfuse v4 compatible)
        obs = client.start_observation(
            name="llm_judge_claude_call",
            as_type="GENERATION",
            model=model,
            metadata={
                "job_id": job_id,
                "run_id": correlation_id,
                "environment": os.getenv("ENV", "development"),
                "system": "careeros"
            }
        )

        obs.update(
            output={
                "match_score": match_score,
                "is_match": is_match,
                "profile_type": profile_type
            },
            usage_details={
                "input": input_tokens,
                "output": output_tokens
            },
            cost_details={
                "total": cost_usd
            },
            metadata={
                "job_id": job_id,
                "run_id": correlation_id,
                "latency_ms": latency_ms,
                "cost_usd": cost_usd,
                "error_category": error_category
            },
            level="ERROR" if not success else "DEFAULT",
            status_message=error
        )
        obs.end()
        client.flush()
    except Exception as exc:
        logger.warning(f"Langfuse trace emission failed (non-blocking): {exc}")

    return trace_data
