import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

def test_all_v2_imports():
    """Verify that all core modules import cleanly without missing dependencies."""
    from src.db import ALLOWED, get_connection
    from src.fact_registry import get_fact_registry
    from src.extraction.job_parser import parse_and_validate_job
    from src.observability.telemetry import PipelineRunTracker, ModelPricingRegistry
    from src.observability.recovery import retry_with_backoff
    from src.evaluation.benchmark import run_benchmark
    from src.ops.healthcheck import run_system_healthcheck

    assert ALLOWED is not None
    assert get_fact_registry() is not None

def test_end_to_end_smoke_pipeline():
    """Verify an end-to-end simulated job passing through V2 ingestion, registry, and telemetry."""
    from src.extraction.job_parser import parse_and_validate_job
    from src.fact_registry import get_fact_registry
    from src.observability.telemetry import PipelineRunTracker

    tracker = PipelineRunTracker(job_id=888)

    # 1. Ingestion & Extraction
    tracker.start_stage("smoke_extraction")
    sample_job = "We are seeking a Molecular Biologist with qPCR and Western blot experience in Munich. Apply to lab@helmholtz.de"
    parsed = parse_and_validate_job(sample_job, "Research Tech", "Helmholtz", "Munich")
    tracker.end_stage("smoke_extraction")

    assert "QPCR" in parsed.required_skills
    assert parsed.application_target.email == "lab@helmholtz.de"

    # 2. Fact Registry Grounding
    tracker.start_stage("smoke_fact_check")
    reg = get_fact_registry()
    qpcr_facts = reg.find_by_entity("qPCR")
    tracker.end_stage("smoke_fact_check")

    assert len(qpcr_facts) >= 1

    # 3. Telemetry Completion
    tracker.complete()
    m = tracker.to_metrics_json()
    assert m["status"] == "COMPLETED"
    assert m["total_duration_ms"] > 0

if __name__ == "__main__":
    test_all_v2_imports()
    test_end_to_end_smoke_pipeline()
    print("Deployment Smoke Test Passed (100% Operational)!")
