"""Experience and education criteria extraction and decision logic.

Evaluates job experience requirements strictly against verified candidate facts loaded
dynamically via adapter. Never invents candidate years or degree completions.
All mandatory constraints must be evaluated and satisfied; industry experience is compared
strictly against industry evidence. Empty description routes to review.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from src.eligibility.models import CriterionDecision, CriterionStatus
from src.eligibility.profile_adapter import (
    compute_profile_version,
    get_candidate_profile_facts,
)

EXPERIENCE_RULE_VERSION = "1.3.0"


def normalize_text(text: str) -> str:
    return (
        (text or "")
        .replace("’", "'")
        .replace("‘", "'")
        .replace("–", "-")
        .replace("—", "-")
        .replace("\u00a0", " ")
    )


def extract_mandatory_industry_experience_years(text: str) -> tuple[Optional[float], list[str]]:
    """Extract mandatory minimum industry/commercial experience years."""
    t = normalize_text(text)
    evidence: list[str] = []
    patterns = [
        r"(?:minimum|at least|mindestens)\s+(\d+(?:\.\d+)?)\s*(?:\+)?\s*years?\s+(?:of\s+)?(?:industry|commercial|pharma|biotech)\s+experience",
        r"(\d+(?:\.\d+)?)\+?\s*years?\s+(?:of\s+)?(?:industry|commercial|pharma|biotech)\s+experience\s+(?:required|mandatory|essential|erforderlich)",
        r"(?:must have|require[s]?)\s+(?:at\s+least\s+)?(\d+(?:\.\d+)?)\+?\s*years?\s+(?:of\s+)?(?:industry|commercial)\s+experience",
    ]
    for pat in patterns:
        for m in re.finditer(pat, t, re.I):
            surrounding = t[max(0, m.start() - 30) : min(len(t), m.end() + 30)]
            if re.search(r"\b(?:preferred|a plus|nice to have|ideally|is a pre|bevorzugt)\b", surrounding, re.I):
                continue
            evidence.append(m.group(0))
            return float(m.group(1)), evidence

    # Non-quantified mandatory industry experience check
    if re.search(r"\b(?:must have|required|essential)\s+industry\s+experience\b|\bindustry\s+experience\s+(?:is\s+)?(?:mandatory|required)\b", t, re.I):
        m_span = re.search(r"\b(?:must have|required|essential)\s+industry\s+experience\b|\bindustry\s+experience\s+(?:is\s+)?(?:mandatory|required)\b", t, re.I)
        evidence.append(m_span.group(0) if m_span else "industry experience required")
        return 1.0, evidence

    return None, evidence


def extract_mandatory_experience_years(text: str) -> tuple[Optional[float], list[str]]:
    """Extract mandatory minimum general experience years."""
    t = normalize_text(text)
    evidence: list[str] = []

    patterns = [
        r"(?:minimum|at least|mindestens|au moins)\s+(\d+(?:\.\d+)?)\s*(?:\+)?\s*(?:-\s*\d+)?\s*years?(?!\s+(?:of\s+)?(?:industry|commercial))",
        r"(\d+(?:\.\d+)?)\+?\s*years?\s+(?:of\s+)?(?:relevant\s+|work\s+|professional\s+)?experience\s+(?:required|mandatory|essential|erforderlich|requis)",
        r"(?:require[s]?|must have|vorausgesetzt)\s+(?:a\s+minimum\s+of\s+)?(\d+(?:\.\d+)?)\+?\s*years?",
        r"(\d+(?:\.\d+)?)\s*-\s*\d+\s*years?\s+(?:of\s+)?(?:working\s+|work\s+)?experience",
    ]

    for pat in patterns:
        for match in re.finditer(pat, t, re.I):
            surrounding = t[max(0, match.start() - 30) : min(len(t), match.end() + 30)]
            if re.search(r"\b(?:preferred|a plus|nice to have|ideally|bevorzugt|souhaité|is a pre)\b", surrounding, re.I):
                continue
            span = match.group(0)
            val = float(match.group(1))
            evidence.append(span)
            return val, evidence

    standalone = re.search(r"\b([1-9]|\d{2,})\+?\s*years?\b", t, re.I)
    if standalone:
        surrounding = t[max(0, standalone.start() - 40) : min(len(t), standalone.end() + 40)]
        if not re.search(r"\b(?:preferred|a plus|nice to have|ideally|is a pre)\b", surrounding, re.I):
            if re.search(r"\b(?:required|mandatory|minimum|experience|proven|track record)\b", surrounding, re.I):
                val = float(standalone.group(1))
                evidence.append(standalone.group(0))
                return val, evidence

    return None, evidence


def extract_preferred_experience_years(text: str) -> tuple[Optional[float], list[str]]:
    """Extract explicitly preferred / nice-to-have experience years."""
    t = normalize_text(text)
    evidence: list[str] = []
    pref_patterns = [
        r"(\d+(?:\.\d+)?)\+?\s*years?.*?\b(?:preferred|a plus|nice to have|ideally|is a pre|bevorzugt)\b",
        r"\b(?:preferred|a plus|nice to have|ideally|is a pre|bevorzugt)\b.*?(\d+(?:\.\d+)?)\+?\s*years?",
    ]
    for pat in pref_patterns:
        match = re.search(pat, t, re.I)
        if match:
            evidence.append(match.group(0))
            return float(match.group(1)), evidence
    return None, evidence


def check_education_barrier(
    title: str,
    text: str,
    candidate: Mapping[str, Any],
) -> tuple[Optional[str], list[str]]:
    """Check for hard education barriers (PhD, Postdoc, Master's thesis student, MLO/vocational degree)."""
    norm_title = normalize_text(title)
    norm_text = normalize_text(text)
    combined = f"{norm_title}\n{norm_text}"

    # 1. Postdoc
    if re.search(r"\bpost\s*doc(?:toral)?\b|\bpost-doctoral\b", norm_title, re.I):
        return "Postdoc role required", [norm_title]
    postdoc_req = re.search(r"\b(?:at least|minimum)\s+\d+\s+years?\s+of\s+postdoctoral\s+experience\b", norm_text, re.I)
    if postdoc_req:
        return "Postdoctoral experience required", [postdoc_req.group(0)]

    # 2. Master's programme enrollment required
    m_enroll = re.search(
        r"\b(?:enrolled|enrollment)\s+in\s+a\s+master'?s?\s+(?:programme|program|degree)\b|\bmaster'?s?\s+thesis\s+(?:student|project)?\b",
        combined,
        re.I,
    )
    if m_enroll:
        enrolled = candidate.get("enrolled_programs") or []
        is_master = any("master" in str(p).lower() or "m.sc" in str(p).lower() for p in enrolled)
        if not is_master:
            return "Enrolled Master's student required (candidate is undergraduate B.Sc. student)", [m_enroll.group(0)]

    # 3. Completed PhD required without Master/Bachelor alternative
    has_phd = bool(re.search(r"\bph\s*\.?\s*d\s*\.?\b|\bphd\b|\bdoctorate\b|\bdoctoral\b", combined, re.I))
    if has_phd:
        has_alt = bool(re.search(r"\b(?:or|equivalent|alternatively|master'?s?|bachelor'?s?|m\.?sc|b\.?sc)\b", norm_text, re.I))
        if re.search(r"\bph\.?d\.?\s+(?:scientist|chemist|biologist|researcher|engineer|fellow)\b", norm_title, re.I) and not has_alt:
            return "PhD required without alternative", [norm_title]
        if re.search(r"\b(?:ph\.?d\.?|phd)\s+(?:is\s+)?(?:mandatory|required)\b", norm_text, re.I) and not has_alt:
            return "PhD mandatory", ["PhD mandatory"]

    # 4. Mandatory completed vocational degree or MLO degree level 4 requirement (e.g. 4291)
    mlo_match = re.search(r"\bto have an mlo degree(?:\s+level\s+4)?(?:\s+with\s+[\d-]+\s+years?\s+working\s+experience)?\b", norm_text, re.I)
    if mlo_match:
        is_grad = candidate.get("is_graduated")
        has_mlo = any("mlo" in str(deg).lower() for deg in (candidate.get("completed_degrees") or []))
        if not has_mlo and is_grad is not True:
            return "MLO degree / completed vocational technician degree required (candidate is undergraduate student)", [mlo_match.group(0)]

    # 5. Mandatory completed Bachelor's degree
    is_internship = bool(re.search(r"\bintern(?:ship)?\b|\bstagiaire\b|\bpraktikant\b|\bstudent\b", norm_title, re.I))
    if not is_internship:
        req_completed_bsc = re.search(
            r"\bcompleted\s+(?:bachelor'?s?|b\.?sc)\b|\b(?:must\s+have|requires)\s+(?:a\s+)?completed\s+degree\b",
            norm_text,
            re.I,
        )
        if req_completed_bsc:
            is_grad = candidate.get("is_graduated")
            if is_grad is False:
                return "Completed degree required (candidate is currently enrolled)", [req_completed_bsc.group(0)]
            if is_grad is None:
                return "Completed degree required (candidate graduation status unknown)", [req_completed_bsc.group(0)]

    return None, []


def evaluate_experience_eligibility(
    title: str,
    description: str,
    experience_requirements: str = "",
    education_requirements: str = "",
    candidate: Optional[Mapping[str, Any]] = None,
) -> CriterionDecision:
    """Evaluate candidate experience and education eligibility against verified facts.

    All mandatory constraints must be checked. Empty job text routes to review.
    """
    now_str = datetime.now(timezone.utc).isoformat()
    if candidate is None:
        candidate = get_candidate_profile_facts()
    prof_ver = str(candidate.get("profile_version") or compute_profile_version(candidate))

    text = f"{experience_requirements}\n{education_requirements}\n{description}".strip()

    # Empty or insufficient job description must be review
    if len(f"{title} {text}".strip()) < 15:
        return CriterionDecision(
            status=CriterionStatus.UNKNOWN,
            reason="Empty or insufficient job description; requires manual review.",
            evidence_spans=[],
            checked_at=now_str,
            rule_version=EXPERIENCE_RULE_VERSION,
            profile_version=prof_ver,
        )

    unmet_reasons: list[str] = []
    unknown_reasons: list[str] = []
    all_evidence: list[str] = []

    # 1. Hard education barriers
    edu_barrier, edu_evidence = check_education_barrier(title, text, candidate)
    if edu_barrier:
        all_evidence.extend(edu_evidence)
        if "unknown" in edu_barrier:
            unknown_reasons.append(edu_barrier)
        else:
            unmet_reasons.append(edu_barrier)

    # 2. Mandatory industry experience check (strictly compared to industry evidence)
    req_ind_years, ind_evidence = extract_mandatory_industry_experience_years(text)
    if req_ind_years is not None:
        all_evidence.extend(ind_evidence)
        cand_ind = candidate.get("industry_experience_years")
        if cand_ind is None:
            unknown_reasons.append(f"Requires {req_ind_years:g}+ years industry experience; candidate industry duration is unverified/unknown.")
        elif cand_ind < req_ind_years:
            unmet_reasons.append(f"Requires {req_ind_years:g}+ years industry experience; verified profile documents {cand_ind:g} years industry experience.")

    # 3. Mandatory general experience check (compared to total work experience)
    min_years, min_evidence = extract_mandatory_experience_years(text)
    if min_years is not None:
        all_evidence.extend(min_evidence)
        cand_total = candidate.get("total_work_experience_years")
        if cand_total is None:
            unknown_reasons.append(f"Requires {min_years:g}+ years mandatory experience; candidate total experience duration is unverified/unknown.")
        elif cand_total < min_years:
            unmet_reasons.append(f"Requires {min_years:g}+ years mandatory experience; verified profile documents {cand_total:g} years total experience.")

    if unmet_reasons:
        return CriterionDecision(
            status=CriterionStatus.UNMET,
            reason=" | ".join(unmet_reasons),
            evidence_spans=all_evidence,
            checked_at=now_str,
            rule_version=EXPERIENCE_RULE_VERSION,
            profile_version=prof_ver,
        )

    if unknown_reasons:
        return CriterionDecision(
            status=CriterionStatus.UNKNOWN,
            reason=" | ".join(unknown_reasons),
            evidence_spans=all_evidence,
            checked_at=now_str,
            rule_version=EXPERIENCE_RULE_VERSION,
            profile_version=prof_ver,
        )

    # 4. Preferred experience note
    pref_years, pref_evidence = extract_preferred_experience_years(text)
    if pref_years is not None:
        return CriterionDecision(
            status=CriterionStatus.MET,
            reason=f"Candidate meets mandatory experience baseline; {pref_years:g} years is preferred, not mandatory.",
            evidence_spans=pref_evidence,
            checked_at=now_str,
            rule_version=EXPERIENCE_RULE_VERSION,
            profile_version=prof_ver,
        )

    return CriterionDecision(
        status=CriterionStatus.MET,
        reason="No prohibitive mandatory experience or degree barrier detected.",
        evidence_spans=[],
        checked_at=now_str,
        rule_version=EXPERIENCE_RULE_VERSION,
        profile_version=prof_ver,
    )
