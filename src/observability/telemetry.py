import os
import re
import time
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Dict, Optional, Any, List

class ModelPricingRegistry:
    """Dynamic pricing lookup table per 1M tokens to avoid hardcoded pricing."""
    PRICING_TABLE = {
        "claude-3-5-sonnet": {"input_per_1m": 3.00, "output_per_1m": 15.00},
        "claude-3-7-sonnet": {"input_per_1m": 3.00, "output_per_1m": 15.00},
        "claude-3-haiku": {"input_per_1m": 0.25, "output_per_1m": 1.25},
        "gpt-4o": {"input_per_1m": 2.50, "output_per_1m": 10.00},
        "gpt-4o-mini": {"input_per_1m": 0.15, "output_per_1m": 0.60},
        "gemini": {"input_per_1m": 0.075, "output_per_1m": 0.30},
        "gemini-flash": {"input_per_1m": 0.075, "output_per_1m": 0.30},
        "deepseek-v4p1-flash": {"input_per_1m": 0.22, "output_per_1m": 0.66},
        "default": {"input_per_1m": 3.00, "output_per_1m": 15.00}
    }

    @classmethod
    def calculate_cost(cls, model_name: str, input_tokens: int, output_tokens: int) -> float:
        clean_name = "default"
        for key in cls.PRICING_TABLE:
            if key in model_name.lower():
                clean_name = key
                break
        rate = cls.PRICING_TABLE[clean_name]
        cost = (input_tokens / 1_000_000.0) * rate["input_per_1m"] + (output_tokens / 1_000_000.0) * rate["output_per_1m"]
        return round(cost, 6)

class PipelineRunTracker:
    """
    Cross-Cutting Telemetry & Observability Tracker for Job Pipeline Runs.
    Measures stage latencies, token consumption, dynamic costs, and failure/retry states.
    """
    VERSION_METADATA = {
        "generator_version": "2.0.0",
        "prompt_version": "2.0",
        "master_cv_version": "2.0",
        "verifier_version": "3.0",
        "system_architecture": "V2_PRODUCTION_GRADE"
    }

    def __init__(self, job_id: int):
        self.trace_id = str(uuid.uuid4())[:8]
        self.job_id = job_id
        self.start_time = time.perf_counter()
        self.stage_starts: dict[str, float] = {}
        self.stage_latencies_ms: dict[str, float] = {}
        self.events: list[dict] = []
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost_usd = 0.0
        self.status = "RUNNING" # RUNNING, RETRYING, COMPLETED, FAILED
        self.retry_count = 0
        self.last_error: Optional[str] = None

    def start_stage(self, stage_name: str):
        self.stage_starts[stage_name] = time.perf_counter()

    def end_stage(self, stage_name: str, tokens_in: int = 0, tokens_out: int = 0, model: str = "claude-3-5-sonnet", error: Optional[str] = None):
        start = self.stage_starts.get(stage_name, time.perf_counter())
        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        self.stage_latencies_ms[stage_name] = duration_ms

        stage_cost = ModelPricingRegistry.calculate_cost(model, tokens_in, tokens_out)
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.cost_usd = round(self.cost_usd + stage_cost, 5)

        if error:
            self.last_error = error
            self.status = "FAILED"

        self.events.append({
            "stage": stage_name,
            "duration_ms": duration_ms,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": stage_cost,
            "error": error
        })

    def complete(self):
        if self.status != "FAILED":
            self.status = "COMPLETED"
        total_duration_ms = round((time.perf_counter() - self.start_time) * 1000, 1)
        self.stage_latencies_ms["total_pipeline_ms"] = total_duration_ms

    def to_metrics_json(self) -> Dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "job_id": self.job_id,
            "status": self.status,
            "total_duration_ms": self.stage_latencies_ms.get("total_pipeline_ms", 0),
            "stage_latencies_ms": self.stage_latencies_ms,
            "total_tokens": self.tokens_in + self.tokens_out,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": self.cost_usd,
            "retry_count": self.retry_count,
            "last_error": self.last_error,
            "events_count": len(self.events),
            "recorded_at": datetime.now(timezone.utc).isoformat()
        }

DEFAULT_FIREWORKS_MODEL = "accounts/fireworks/models/deepseek-v4p1-flash"
FIREWORKS_INPUT_RATE_PER_MILLION = 0.22
FIREWORKS_OUTPUT_RATE_PER_MILLION = 0.66
_TELEMETRY_RECORDS: List["TelemetryRecord"] = []


@dataclass
class TelemetryRecord:
    timestamp: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    input_cost: float
    output_cost: float
    total_cost: float
    latency_seconds: float
    caller: str
    success: bool
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def sanitize_text(text: str) -> str:
    """Redact API credentials before logs or telemetry records are emitted."""
    if not text:
        return ""
    sanitized = re.sub(r"(?i)bearer\s+[A-Za-z0-9_\-\.]+", "Bearer [REDACTED]", str(text))
    sanitized = re.sub(
        r"(?i)(api[_-]?key|x-api-key|x-goog-api-key)([:=]\s*)[A-Za-z0-9_\-\.]+",
        r"\1\2[REDACTED]",
        sanitized,
    )
    for env_var in ("FIREWORKS_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        value = os.getenv(env_var)
        if value:
            sanitized = sanitized.replace(value.strip(), "[REDACTED]")
    return sanitized


def safe_log(message: str) -> None:
    print(sanitize_text(message))


def calculate_cost(
    prompt_tokens: int,
    completion_tokens: int,
    input_rate_per_million: Optional[float] = None,
    output_rate_per_million: Optional[float] = None,
) -> Dict[str, Any]:
    """Calculate token costs using the standard Fireworks model rates by default."""
    input_rate_per_million = FIREWORKS_INPUT_RATE_PER_MILLION if input_rate_per_million is None else input_rate_per_million
    output_rate_per_million = FIREWORKS_OUTPUT_RATE_PER_MILLION if output_rate_per_million is None else output_rate_per_million
    prompt_tokens = max(0, int(prompt_tokens or 0))
    completion_tokens = max(0, int(completion_tokens or 0))
    input_cost = prompt_tokens * input_rate_per_million / 1_000_000
    output_cost = completion_tokens * output_rate_per_million / 1_000_000
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "input_cost": round(input_cost, 8),
        "output_cost": round(output_cost, 8),
        "total_cost": round(input_cost + output_cost, 8),
    }


def record_usage(
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_seconds: float = 0.0,
    caller: str = "unknown",
    success: bool = True,
    error: Optional[str] = None,
    input_rate_per_million: Optional[float] = None,
    output_rate_per_million: Optional[float] = None,
) -> TelemetryRecord:
    costs = calculate_cost(prompt_tokens, completion_tokens, input_rate_per_million, output_rate_per_million)
    record = TelemetryRecord(
        timestamp=datetime.now(timezone.utc).isoformat(),
        provider=provider,
        model=model,
        prompt_tokens=costs["prompt_tokens"],
        completion_tokens=costs["completion_tokens"],
        total_tokens=costs["total_tokens"],
        input_cost=costs["input_cost"],
        output_cost=costs["output_cost"],
        total_cost=costs["total_cost"],
        latency_seconds=round(max(0.0, float(latency_seconds or 0.0)), 4),
        caller=caller,
        success=success,
        error=sanitize_text(error) if error else None,
    )
    _TELEMETRY_RECORDS.append(record)
    safe_log(
        f"[Telemetry] caller={caller} provider={provider} model={model} "
        f"tokens={record.total_tokens} cost=${record.total_cost:.6f} success={success}"
    )
    return record


def get_telemetry_records() -> List[TelemetryRecord]:
    return list(_TELEMETRY_RECORDS)


def reset_telemetry() -> None:
    _TELEMETRY_RECORDS.clear()


def get_total_cost() -> float:
    return round(sum(record.total_cost for record in _TELEMETRY_RECORDS), 8)


if __name__ == "__main__":
    tracker = PipelineRunTracker(job_id=999)
    tracker.start_stage("extraction")
    time.sleep(0.05)
    tracker.end_stage("extraction", tokens_in=500, tokens_out=150)

    tracker.start_stage("generation")
    time.sleep(0.1)
    tracker.end_stage("generation", tokens_in=1200, tokens_out=800)

    tracker.complete()
    print("Telemetry Metrics Output:")
    import json
    print(json.dumps(tracker.to_metrics_json(), indent=2))
