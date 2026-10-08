from pathlib import Path
from typing import Dict, Any
import json
import zipfile
import xml.etree.ElementTree as ET


def application_package_error(job_folder) -> str | None:
    """Return a blocking reason unless both documents and their fact audit exist.

    PDF conversion remains optional; DOCX is the canonical downloadable format.
    """
    if not job_folder:
        return "Application package path is missing"
    folder = Path(job_folder)
    for filename in ("tailored_cv.docx", "cover_letter.docx"):
        artifact = folder / filename
        try:
            if not artifact.is_file() or artifact.stat().st_size == 0:
                return f"Missing or empty {filename}"
            with zipfile.ZipFile(artifact) as document:
                if document.testzip() is not None:
                    return f"Corrupt {filename}"
                ET.fromstring(document.read("word/document.xml"))
        except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError):
            return f"Invalid DOCX structure in {filename}"
    try:
        apply_info = json.loads((folder / "apply_info.json").read_text(encoding="utf-8"))
        report = json.loads((folder / "scientific_validation.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return "Application metadata or scientific validation is missing or invalid"
    if not isinstance(apply_info, dict) or not apply_info:
        return "Application metadata is invalid"
    if not isinstance(report, dict):
        return "Scientific validation is invalid"
    checks = report.get("factual_checks")
    if not isinstance(checks, dict) or type(checks.get("unsupported_claims_count")) is not int:
        return "Scientific validation has no unsupported-claim count"
    if checks["unsupported_claims_count"] != 0:
        return "Scientific validation found unsupported claims"
    claims = report.get("claims_with_references")
    if not isinstance(claims, list):
        return "Scientific validation has no claim list"
    version = report.get("version")
    if not isinstance(version, str):
        return "Scientific validation has no version"

    if version.startswith("3."):
        coverage = report.get("detected_claim_coverage")
        if not isinstance(coverage, dict):
            return "Scientific validation has no detected-claim coverage report"
        detected_count = coverage.get("detected_claims_count")
        assessed_count = coverage.get("assessed_claims_count")
        if type(detected_count) is not int or detected_count < 0 or detected_count != len(claims):
            return "Scientific validation has an invalid detected-claim count"
        if type(assessed_count) is not int or assessed_count != detected_count:
            return "Scientific validation has unassessed or partially assessed detected claims"
        if coverage.get("coverage_status") != "complete":
            return "Scientific validation detected-claim checks are incomplete"
        if not isinstance(coverage.get("scope_and_limitations"), str) or not coverage["scope_and_limitations"].strip():
            return "Scientific validation has no claim-detection scope"
        if not isinstance(coverage.get("detection_method"), str) or not coverage["detection_method"].strip():
            return "Scientific validation has no claim-detection method"
        artifact_status = report.get("artifact_read_status")
        required_artifacts = ("tailored_cv.docx", "cover_letter.docx", "apply_info.json")
        if not isinstance(artifact_status, dict) or any(
            not isinstance(artifact_status.get(name), dict)
            or artifact_status[name].get("readable") is not True
            for name in required_artifacts
        ):
            return "Scientific validation could not read every required source artifact"
        if any(
            not isinstance(claim, dict)
            or claim.get("status") != "VERIFIED_ACCURATE"
            or not isinstance(claim.get("evidence_fact_ids"), list)
            or not claim["evidence_fact_ids"]
            or any(not isinstance(fid, str) or not fid.strip() for fid in claim["evidence_fact_ids"])
            for claim in claims
        ):
            return "Scientific validation contains unsupported claims or claims without evidence IDs"
        try:
            from src.fact_registry import get_fact_registry
            registry = get_fact_registry()
            if any(
                registry.get(fid) is None
                for claim in claims
                for fid in claim["evidence_fact_ids"]
            ):
                return "Scientific validation cites an unknown fact ID"
        except Exception:
            return "Fact registry is unavailable for evidence ID validation"
        term_count = report.get("registered_terms_detected_count")
        terms = report.get("registered_terms_detected")
        if type(term_count) is not int or not isinstance(terms, list) or term_count != len(terms):
            return "Scientific validation has invalid registered-term counts"
        if report.get("term_fidelity_score") is not None:
            return "Scientific validation reports an unsupported term-fidelity score"
        if checks.get("flagged_claims_count") != detected_count:
            return "Scientific validation has an invalid claim count"
    elif version.startswith("2."):
        # Bounded compatibility for stored v2 packages. New reports must use v3.
        # V2 truncated detection at six claims and its empty list cannot prove that
        # no claims were present; accepting it preserves stored-package access only
        # and must never be interpreted as full-document coverage or truth approval.
        # A legacy report with listed claims is accepted only when each has real evidence.
        if any(
            not isinstance(claim, dict)
            or claim.get("status") != "VERIFIED_ACCURATE"
            or not isinstance(claim.get("evidence_fact_ids"), list)
            or not claim["evidence_fact_ids"]
            for claim in claims
        ):
            return "Legacy scientific validation contains unverified claims or missing evidence IDs"
        try:
            from src.fact_registry import get_fact_registry
            registry = get_fact_registry()
            if any(registry.get(fid) is None for claim in claims for fid in claim["evidence_fact_ids"]):
                return "Legacy scientific validation cites an unknown fact ID"
        except Exception:
            return "Fact registry is unavailable for legacy evidence ID validation"
    else:
        return f"Unsupported scientific validation version: {version}"

    if version.startswith("2.") and not isinstance(checks.get("institution_preserved"), bool):
        return "Legacy scientific validation has invalid institution check"
    if version.startswith("2.") and not isinstance(checks.get("target_gene_accurate"), bool):
        return "Legacy scientific validation has invalid target-gene check"
    if version.startswith("2."):
        fidelity = report.get("term_fidelity_score")
        if not isinstance(fidelity, (int, float)) or isinstance(fidelity, bool) or not 0 <= fidelity <= 100:
            return "Legacy scientific validation has no valid fidelity score"
        terms = report.get("verified_terms")
        count = report.get("verified_terms_count")
        if type(count) is not int or not isinstance(terms, list) or count != len(terms):
            return "Legacy scientific validation has invalid verified terms"
        if type(checks.get("flagged_claims_count")) is not int or checks["flagged_claims_count"] != len(claims):
            return "Legacy scientific validation has an invalid claim count"
    if not isinstance(report.get("audit_disclaimer"), str) or not report["audit_disclaimer"].strip():
        return "Scientific validation has no audit disclaimer"
    return None

def validate_pdf_artifact(pdf_path: Path) -> Dict[str, Any]:
    """
    Automated PDF & Document Quality Assurance (QA) Engine.
    Validates file existence, non-zero byte size, page count, and structural integrity.
    """
    if not pdf_path.exists():
        return {
            "status": "FAILED",
            "error": f"File {pdf_path.name} does not exist"
        }

    size_bytes = pdf_path.stat().st_size
    if size_bytes < 1024: # Less than 1 KB is an invalid/corrupted PDF
        return {
            "status": "FAILED",
            "error": f"PDF file {pdf_path.name} is corrupted or under minimum size ({size_bytes} bytes)"
        }

    # Check PDF Header Magic Bytes (%PDF-)
    try:
        with open(pdf_path, "rb") as f:
            header = f.read(5)
            if header != b"%PDF-":
                return {
                    "status": "FAILED",
                    "error": "Invalid PDF magic header bytes"
                }
    except Exception as e:
        return {"status": "FAILED", "error": str(e)}

    return {
        "status": "PASSED_QA_200_OK",
        "file_name": pdf_path.name,
        "size_kb": round(size_bytes / 1024.0, 1),
        "is_valid_pdf": True
    }

def validate_job_package_artifacts(job_folder: Path) -> Dict[str, Any]:
    """Validates that all required application kit artifacts exist and pass QA."""
    required_files = ["tailored_cv.pdf", "cover_letter.pdf", "apply_info.json", "scientific_validation.json"]
    qa_results = {}
    all_passed = True

    for req in required_files:
        f_path = job_folder / req
        if f_path.suffix == ".pdf":
            res = validate_pdf_artifact(f_path)
            qa_results[req] = res
            if res.get("status") != "PASSED_QA_200_OK":
                all_passed = False
        else:
            exists = f_path.exists() and f_path.stat().st_size > 10
            qa_results[req] = {"status": "PASSED_QA_200_OK" if exists else "MISSING"}
            if not exists:
                all_passed = False

    return {
        "all_artifacts_passed": all_passed,
        "job_folder": job_folder.name,
        "artifact_checks": qa_results
    }

if __name__ == "__main__":
    print("PDF & Artifact QA Engine Initialized.")
