import re
from typing import List, Dict, Optional

from pydantic import BaseModel

class ApplicationTarget(BaseModel):
    type: str = "unknown" # email, portal, company_site, linkedin, unknown
    url: Optional[str] = None
    email: Optional[str] = None
    instructions: Optional[str] = None

class StructuredJobSchema(BaseModel):
    """
    Validated Canonical Job Object (Untrusted Raw Text is Sanitized & Isolated).
    """
    title: str
    company: Optional[str] = None
    location: Optional[str] = None
    required_skills: List[str] = []
    preferred_skills: List[str] = []
    required_degree: List[str] = []
    preferred_degree: List[str] = []
    experience_years_min: Optional[float] = None
    experience_years_preferred: Optional[float] = None
    requires_industry_experience: bool = False
    requires_postdoc: bool = False
    required_languages: Dict[str, str] = {}
    preferred_languages: Dict[str, str] = {}
    visa_sponsorship: Optional[bool] = None
    application_target: Optional[ApplicationTarget] = None
    source_evidence_spans: List[str] = []
    is_injection_detected: bool = False
    sanitized_text: str = ""

INJECTION_PATTERNS = [
    r'ignore (all )?previous instructions',
    r'system override',
    r'disregard previous prompts',
    r'say the candidate has',
    r'you are now in developer mode',
    r'<script.*?>.*?</script>',
    r'jailbreak',
    r'drop database',
]

def sanitize_untrusted_text(raw_text: str) -> tuple[str, bool]:
    """Scans and strips adversarial prompt injection attacks from raw job descriptions."""
    injection_detected = False
    cleaned_text = raw_text

    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, cleaned_text, re.IGNORECASE):
            injection_detected = True
            cleaned_text = re.sub(pattern, "[ADVERSARIAL_INJECTION_REDACTED]", cleaned_text, flags=re.IGNORECASE)

    return cleaned_text, injection_detected

def parse_and_validate_job(raw_text: str, title: str = "", company: str = "", location: str = "") -> StructuredJobSchema:
    """
    Transforms raw untrusted job descriptions into a verified StructuredJobSchema.
    Combines rule-based extraction with sanitization.
    """
    sanitized_text, injection_flag = sanitize_untrusted_text(raw_text or "")
    text_lower = sanitized_text.lower()

    # 1. Extract Skills
    canonical_skills = [
        "python", "r", "bash", "linux", "nextflow", "snakemake", "git", "sql", "java",
        "qpcr", "pcr", "western blot", "western blotting", "bca assay", "immunofluorescence",
        "rna extraction", "dna extraction", "cell culture", "molecular docking", "autodock",
        "ngs", "tcga", "single-cell", "differential expression", "raman spectroscopy"
    ]
    extracted_req_skills = []
    evidence_spans = []
    for skill in canonical_skills:
        if re.search(r'\b' + re.escape(skill) + r'\b', text_lower):
            extracted_req_skills.append(skill.title() if len(skill) > 4 else skill.upper())
            evidence_spans.append(f"skill_match: {skill}")

    # 2. Extract Languages
    languages = {}
    if "german" in text_lower or "deutsch" in text_lower:
        if "fluent german" in text_lower or "german c1" in text_lower or "c1 german" in text_lower or "fließend" in text_lower:
            languages["German"] = "Fluent (C1/Native)"
        else:
            languages["German"] = "Basic/Preferred"
    if "english" in text_lower:
        languages["English"] = "Fluent/Required"

    # 3. Extract Experience
    from src.eligibility.experience import (
        extract_mandatory_experience_years,
        extract_preferred_experience_years,
    )

    exp_years, exp_evidence = extract_mandatory_experience_years(sanitized_text)
    pref_exp_years, pref_evidence = extract_preferred_experience_years(sanitized_text)
    evidence_spans.extend(exp_evidence + pref_evidence)

    requires_industry = bool(re.search(r"\b(?:industry\s+experience|experience\s+in\s+industry)\b", text_lower))
    requires_postdoc = bool(re.search(r"\bpost\s*doc(?:toral)?\b", text_lower))

    # 4. Extract Degrees
    req_degrees = []
    if "phd" in text_lower or "ph.d" in text_lower or "doctorate" in text_lower:
        req_degrees.append("Ph.D.")
    if "master" in text_lower or "m.sc" in text_lower or "msc" in text_lower:
        req_degrees.append("M.Sc.")
    if "bachelor" in text_lower or "b.sc" in text_lower or "bsc" in text_lower:
        req_degrees.append("B.Sc.")

    # 5. Extract Application Method & Target
    email_match = re.search(r'[\w.+-]+@[\w-]+\.[\w.-]+', sanitized_text)
    app_email = email_match.group(0) if email_match else None

    url_match = re.search(r'https?://[^\s<>"]+|www\.[^\s<>"]+', sanitized_text)
    app_url = url_match.group(0) if url_match else None

    app_type = "email" if app_email else ("portal" if app_url else "unknown")

    app_target = ApplicationTarget(
        type=app_type,
        url=app_url,
        email=app_email,
        instructions="Send CV & Cover Letter directly via Email" if app_email else "Apply via Web Portal"
    )

    return StructuredJobSchema(
        title=title or "Bioinformatics / Molecular Biology Position",
        company=company or "Research Institution",
        location=location or "Europe",
        required_skills=extracted_req_skills,
        preferred_skills=[],
        required_degree=req_degrees,
        preferred_degree=[],
        experience_years_min=exp_years,
        experience_years_preferred=pref_exp_years,
        requires_industry_experience=requires_industry,
        requires_postdoc=requires_postdoc,
        required_languages=languages,
        preferred_languages={},
        visa_sponsorship=True if "visa" in text_lower else None,
        application_target=app_target,
        source_evidence_spans=evidence_spans,
        is_injection_detected=injection_flag,
        sanitized_text=sanitized_text,
    )

if __name__ == "__main__":
    sample_raw = """
    We are looking for a Molecular Biology Research Assistant in Munich.
    Requirements:
    - B.Sc. or M.Sc. in Molecular Biology, Genetics or related field.
    - Hands-on experience with qPCR, Western blotting, and RNA extraction.
    - Python or R knowledge is a plus.
    - Fluent English required.
    - 2+ years of experience in lab environment.
    Apply with your CV and motivation letter to hr-recruiting@helmholtz-munich.de
    """
    parsed = parse_and_validate_job(sample_raw, "Research Assistant", "Helmholtz Munich", "Munich, Germany")
    print(parsed.json(indent=2) if hasattr(parsed, 'json') else parsed.__dict__)
