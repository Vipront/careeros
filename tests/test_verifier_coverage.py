import json
import zipfile

from src.documents import verifier


class _EmptyRegistry:
    facts = {}

    @staticmethod
    def match_claim_to_facts(_claim):
        return []

    @staticmethod
    def get(_fact_id):
        return None


class _Fact:
    def __init__(self, text, metrics=()):
        self.text = text
        self.metrics = list(metrics)


class _CandidateRegistry:
    def __init__(self, fact):
        self.facts = {"FACT_01": fact}

    @staticmethod
    def match_claim_to_facts(_claim):
        return ["FACT_01"]

    def get(self, fact_id):
        return self.facts.get(fact_id)


def _write_docx(path, text):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", f"<document><body><p>{text}</p></body></document>")


def _write_inputs(folder, cv_text="Ordinary profile text.", cover_text="Cover letter text."):
    _write_docx(folder / "tailored_cv.docx", cv_text)
    _write_docx(folder / "cover_letter.docx", cover_text)
    (folder / "apply_info.json").write_text(json.dumps({"instructions": "Apply online"}), encoding="utf-8")


def test_claim_extraction_keeps_every_detected_sentence(monkeypatch):
    monkeypatch.setattr(verifier, "get_fact_registry", lambda: _EmptyRegistry())
    sentences = [f"Binding affinity measured at {10 + index} nM for unknown sample number {index}." for index in range(9)]

    claims = verifier.extract_hard_scientific_claims(" ".join(sentences))

    assert len(claims) == 9
    assert all(claim["status"] == "FLAGGED_UNVERIFIED_CLAIM" for claim in claims)
    assert all(claim["evidence_fact_ids"] == [] for claim in claims)


def test_shared_word_overlap_cannot_support_a_different_metric(monkeypatch):
    fact = _Fact("The lead candidate had binding affinity of -11.9 kcal/mol in CXCR4 molecular docking.", [-11.9])
    monkeypatch.setattr(verifier, "get_fact_registry", lambda: _CandidateRegistry(fact))

    claims = verifier.extract_hard_scientific_claims(
        "The lead candidate had binding affinity of -999 kcal/mol in CXCR4 molecular docking."
    )

    assert claims[0]["status"] == "FLAGGED_UNVERIFIED_CLAIM"
    assert claims[0]["evidence_fact_ids"] == []


def test_integer_and_comma_decimal_metric_values_are_compared_numerically(monkeypatch):
    fact = _Fact("The candidate concentration reached 999,0 nM during assay.")
    monkeypatch.setattr(verifier, "get_fact_registry", lambda: _CandidateRegistry(fact))

    claims = verifier.extract_hard_scientific_claims(
        "The candidate concentration reached 999 nM during assay."
    )

    assert claims[0]["status"] == "VERIFIED_ACCURATE"
    assert claims[0]["evidence_fact_ids"] == ["FACT_01"]


def test_no_detected_claims_has_no_fidelity_score_or_full_truth_claim(monkeypatch, tmp_path):
    monkeypatch.setattr(verifier, "get_fact_registry", lambda: _EmptyRegistry())
    _write_inputs(tmp_path)

    report = verifier.verify_tailored_document(tmp_path, "unused master CV")

    assert report["term_fidelity_score"] is None
    assert report["detected_claim_coverage"]["detected_claims_count"] == 0
    assert report["detected_claim_coverage"]["assessed_claims_count"] == 0
    assert "does not establish full-document truth" in report["detected_claim_coverage"]["scope_and_limitations"]
    assert "does not certify every document statement" in report["audit_disclaimer"]


def test_unreadable_document_makes_claim_coverage_incomplete(monkeypatch, tmp_path):
    monkeypatch.setattr(verifier, "get_fact_registry", lambda: _EmptyRegistry())
    (tmp_path / "tailored_cv.docx").write_bytes(b"not a docx")
    _write_docx(tmp_path / "cover_letter.docx", "Ordinary cover letter.")
    (tmp_path / "apply_info.json").write_text(json.dumps({"instructions": "Apply online"}), encoding="utf-8")

    report = verifier.verify_tailored_document(tmp_path, "unused master CV")

    assert report["artifact_read_status"]["tailored_cv.docx"]["readable"] is False
    assert report["factual_checks"]["artifacts_readable"] is False
    assert report["detected_claim_coverage"]["coverage_status"] == "incomplete"


def test_institution_and_gene_indicators_are_informational(monkeypatch, tmp_path):
    monkeypatch.setattr(verifier, "get_fact_registry", lambda: _EmptyRegistry())
    _write_inputs(tmp_path, "A generic profile with ordinary text.")

    report = verifier.verify_tailored_document(tmp_path, "unused master CV")

    assert report["factual_checks"]["institution_preserved"] is False
    assert report["factual_checks"]["target_gene_accurate"] is False
    assert report["audit_disclaimer"]
