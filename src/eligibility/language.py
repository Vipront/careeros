"""Language eligibility and posting language preference evaluation logic.

Separates:
1. Posting language preference (strictly EN/TR vs other languages; no automatic waivers).
2. Explicit mandatory working languages (covering all languages including EN/TR)
   with required CEFR/proficiency level comparison.
Preferred language context is never treated as mandatory and never suppresses mandatory requirements.
Absence of stated level means unspecified requirement; candidate unspecified level remains review.
Missing recorded language is unknown unless explicitly verified absent.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from src.eligibility.models import CriterionDecision, CriterionStatus
from src.eligibility.profile_adapter import (
    compute_profile_version,
    get_candidate_profile_facts,
    parse_cefr_level,
)

LANGUAGE_RULE_VERSION = "1.2.0"

DEFAULT_ACCEPTED_POSTING_LANGUAGES = {"english", "en", "turkish", "tr"}


def detect_posting_language(text: str) -> tuple[str, float]:
    """Detect primary written language of the posting text via distinctive vocabulary."""
    t = text.lower()
    if not t.strip():
        return "unknown", 0.0

    french_markers = [
        r"\bvous\b", r"\bvotre\b", r"\bavec\b", r"\bdans\b", r"\bpour\b",
        r"\bune\b", r"\bdes\b", r"\bpar\b", r"\bsont\b", r"\best\b",
        r"\bposte\b", r"\bcandidat\b", r"\bmissions\b", r"\bprofil\b",
        r"\brejoindre\b", r"\bréalisera\b", r"\bformation\b", r"\bnotre\b",
    ]
    german_markers = [
        r"\buns\b", r"\bwir\b", r"\bihre\b", r"\bmit\b", r"\bfür\b",
        r"\beine\b", r"\bder\b", r"\bdie\b", r"\bdas\b", r"\bund\b",
        r"\baufgaben\b", r"\banforderungen\b", r"\bbewerbung\b",
    ]
    dutch_markers = [
        r"\bvan\b", r"\bhet\b", r"\been\b", r"\bvoor\b", r"\bmet\b",
        r"\bje\b", r"\bjouw\b", r"\bwij\b", r"\bzoeken\b", r"\bfunctie\b",
    ]
    turkish_markers = [
        r"\bve\b", r"\biçin\b", r"\bbir\b", r"\bile\b", r"\baranan\b",
        r"\bnitelikler\b", r"\bpozisyon\b", r"\biş\b", r"\btanımı\b",
    ]
    english_markers = [
        r"\bthe\b", r"\band\b", r"\bwith\b", r"\bfor\b", r"\byou\b",
        r"\brequirements\b", r"\bresponsibilities\b", r"\bexperience\b",
        r"\bare\b", r"\bthis\b", r"\bteam\b", r"\brole\b", r"\bjoin\b",
        r"\bseeking\b", r"\btalented\b", r"\bqualifications\b", r"\bin\b",
        r"\bour\b", r"\bwill\b", r"\bto\b",
    ]

    counts = [
        ("french", sum(1 for pat in french_markers if re.search(pat, t))),
        ("german", sum(1 for pat in german_markers if re.search(pat, t))),
        ("dutch", sum(1 for pat in dutch_markers if re.search(pat, t))),
        ("turkish", sum(1 for pat in turkish_markers if re.search(pat, t))),
        ("english", sum(1 for pat in english_markers if re.search(pat, t))),
    ]
    counts.sort(key=lambda x: x[1], reverse=True)
    top_lang, top_score = counts[0]

    if top_score >= 2:
        return top_lang, min(1.0, top_score / 5.0)

    return "unknown", 0.0


def evaluate_posting_language_preference(
    text: str,
    accepted_languages: Optional[set[str]] = None,
    candidate: Optional[Mapping[str, Any]] = None,
) -> CriterionDecision:
    """Evaluate whether the job posting language complies with user preference (EN/TR).

    Strict preference: does NOT waive non-EN/TR posting merely because an international environment exists.
    """
    now_str = datetime.now(timezone.utc).isoformat()
    prof_ver = str(candidate.get("profile_version") or compute_profile_version(candidate)) if candidate else "1.0.0"
    if accepted_languages is None:
        accepted_languages = DEFAULT_ACCEPTED_POSTING_LANGUAGES

    lang, conf = detect_posting_language(text)
    if lang == "unknown":
        return CriterionDecision(
            status=CriterionStatus.UNKNOWN,
            reason="Posting language could not be determined with sufficient confidence.",
            evidence_spans=[],
            checked_at=now_str,
            rule_version=LANGUAGE_RULE_VERSION,
            profile_version=prof_ver,
        )

    if lang in accepted_languages:
        return CriterionDecision(
            status=CriterionStatus.MET,
            reason=f"Posting language ({lang.title()}) matches accepted preferences.",
            evidence_spans=[f"Detected posting language: {lang}"],
            checked_at=now_str,
            rule_version=LANGUAGE_RULE_VERSION,
            profile_version=prof_ver,
        )

    return CriterionDecision(
        status=CriterionStatus.UNMET,
        reason=f"Posting language is {lang.title()}; user preferences require English or Turkish postings.",
        evidence_spans=[f"Posting language detected: {lang}"],
        checked_at=now_str,
        rule_version=LANGUAGE_RULE_VERSION,
        profile_version=prof_ver,
    )


def extract_mandatory_working_languages(text: str) -> list[tuple[str, Optional[str], str]]:
    """Extract explicit mandatory working language requirements from job description.

    Returns list of (language_name, required_level_str_or_None, evidence_span).
    Does NOT invent B2 when no level is stated.
    Preferred language in a separate clause does NOT suppress mandatory languages.
    """
    t = text.lower()
    mandatory: list[tuple[str, Optional[str], str]] = []

    # Patterns for each language family
    language_patterns = [
        # English
        (r"\b(?:fluent|proficient|c1|c2|advanced)\s+(?:in\s+)?english\s+(?:is\s+)?(?:required|mandatory|essential|must)\b", "English", "C1"),
        (r"\benglish\s+(?:level\s+)?(c[12]|b[12])\s+(?:required|mandatory|essential)\b", "English", r"\1"),
        (r"\b(?:professional\s+working\s+proficiency|working\s+proficiency|b2)\s+(?:in\s+)?english\s+(?:is\s+)?(?:required|mandatory|essential)\b", "English", "B2"),
        (r"\benglish\s+(?:is\s+)?(?:required|mandatory|essential)\b", "English", None),
        (r"\bfließende\s+englischkenntnisse\s+(?:erforderlich|vorausgesetzt)\b", "English", "C1"),
        (r"\banglais\s+courant\s+(?:exigé|requis|indispensable)\b", "English", "C1"),
        (r"\bmaîtrise\s+de\s+l'anglais\s+(?:exigée|requise)\b", "English", "C1"),

        # Turkish
        (r"\b(?:akıcı|anadil|ileri\s+düzeyde)\s+türkçe\s+(?:şart|zorunlu|gereklidir)\b", "Turkish", "C1"),
        (r"\btürkçe\s+(?:bilmek\s+)?(?:şart|zorunlu|gerekmektedir)\b", "Turkish", None),

        # French
        (r"\b(?:très\s+bon\s+niveau\s+de\s+|maîtrise\s+du\s+|courant\s+en\s+|niveau\s+c[12]\s+en\s+)français\b", "French", "C1"),
        (r"\bfluent\s+(?:in\s+)?french\s+(?:required|mandatory|essential)\b", "French", "C1"),
        (r"\bfrench\s+(?:c[12]|fluent)\s+required\b", "French", "C1"),
        (r"\bfrançais\s+(?:obligatoire|exigé|indispensable|requis)\b", "French", None),
        (r"\bfrench\s+(?:is\s+)?(?:required|mandatory|essential)\b", "French", None),

        # German
        (r"\b(?:verhandlungssichere?|fließende?)\s+deutschkenntnisse\s*(?:erforderlich|vorausgesetzt|zwingend)?\b", "German", "C1"),
        (r"\bfluent\s+(?:in\s+)?german\s+(?:required|mandatory|essential)\b", "German", "C1"),
        (r"\bgerman\s+(?:c[12]|fluent)\s+required\b", "German", "C1"),
        (r"\bdeutsch\s+(?:erforderlich|vorausgesetzt|zwingend|c[12])\b", "German", None),
        (r"\bgerman\s+(?:is\s+)?(?:required|mandatory|essential)\b", "German", None),

        # Dutch
        (r"\bvloeiend\s+nederlands\s*(?:vereist|verplicht)?\b", "Dutch", "C1"),
        (r"\bfluent\s+(?:in\s+)?dutch\s+(?:required|mandatory|essential)\b", "Dutch", "C1"),
        (r"\bnederlands\s+(?:vereist|verplicht|vloeiend)\b", "Dutch", None),
        (r"\bdutch\s+(?:is\s+)?(?:required|mandatory|essential)\b", "Dutch", None),
    ]

    for pat, lang, default_lvl in language_patterns:
        for match in re.finditer(pat, t):
            span = match.group(0)
            surrounding = t[max(0, match.start() - 30) : min(len(t), match.end() + 30)]
            if re.search(r"\b(?:preferred|a plus|nice to have|ideally|von vorteil|souhaité|un atout|een pré)\b", surrounding):
                continue
            req_lvl = default_lvl
            if default_lvl and r"\1" in default_lvl and match.groups():
                req_lvl = match.group(1).upper()
            mandatory.append((lang, req_lvl, span))
            break

    return mandatory


def extract_preferred_languages(text: str) -> list[tuple[str, str]]:
    """Extract languages explicitly marked as preferred / nice to have."""
    t = text.lower()
    preferred: list[tuple[str, str]] = []
    patterns = [
        (r"\b(?:french|français)\s+(?:is\s+)?(?:a\s+plus|preferred|nice to have|souhaité|un atout)\b", "French"),
        (r"\b(?:german|deutsch)\s+(?:is\s+)?(?:a\s+)?(?:plus|preferred|nice to have|von vorteil)\b", "German"),
        (r"\b(?:dutch|nederlands)\s+(?:is\s+)?(?:a\s+plus|preferred|nice to have|een pré)\b", "Dutch"),
        (r"\b(?:english|anglais|englisch)\s+(?:is\s+)?(?:a\s+plus|preferred|nice to have)\b", "English"),
    ]
    for pat, lang in patterns:
        m = re.search(pat, t)
        if m:
            preferred.append((lang, m.group(0)))
    return preferred


def evaluate_language_eligibility(
    text: str,
    candidate: Optional[Mapping[str, Any]] = None,
) -> CriterionDecision:
    """Evaluate whether candidate meets explicit mandatory working languages and required CEFR level.

    Missing recorded language is unknown unless verified absent.
    Unspecified requirement with unspecified candidate level is unknown/review.
    """
    if candidate is None:
        candidate = get_candidate_profile_facts()
    prof_ver = str(candidate.get("profile_version") or compute_profile_version(candidate))

    now_str = datetime.now(timezone.utc).isoformat()
    cand_languages = candidate.get("languages")
    if cand_languages is None:
        return CriterionDecision(
            status=CriterionStatus.UNKNOWN,
            reason="Candidate language proficiencies are not recorded in verified profile.",
            evidence_spans=[],
            checked_at=now_str,
            rule_version=LANGUAGE_RULE_VERSION,
            profile_version=prof_ver,
        )

    norm_cand_langs: dict[str, str] = {k.lower(): v for k, v in cand_languages.items()}
    explicitly_absent: set[str] = {s.lower() for s in candidate.get("absent_languages", [])}

    mandatory = extract_mandatory_working_languages(text)
    if not mandatory:
        pref = extract_preferred_languages(text)
        if pref:
            return CriterionDecision(
                status=CriterionStatus.MET,
                reason=f"No mandatory working language barrier; {', '.join(p[0] for p in pref)} is preferred, not mandatory.",
                evidence_spans=[p[1] for p in pref],
                checked_at=now_str,
                rule_version=LANGUAGE_RULE_VERSION,
                profile_version=prof_ver,
            )
        return CriterionDecision(
            status=CriterionStatus.MET,
            reason="No prohibitive working language requirement detected.",
            evidence_spans=[],
            checked_at=now_str,
            rule_version=LANGUAGE_RULE_VERSION,
            profile_version=prof_ver,
        )

    unmet_languages = []
    unknown_languages = []
    evidence_spans = []

    for lang, req_lvl_str, span in mandatory:
        lang_lower = lang.lower()
        evidence_spans.append(span)

        if lang_lower not in norm_cand_langs:
            if lang_lower in explicitly_absent:
                unmet_languages.append(f"{lang} (verified absent from candidate skills)")
            else:
                unknown_languages.append(f"{lang} (proficiency is not recorded in candidate profile)")
            continue

        cand_lvl_str = norm_cand_langs[lang_lower]
        cand_score = parse_cefr_level(cand_lvl_str)
        req_score = parse_cefr_level(req_lvl_str) if req_lvl_str else None

        if cand_score is None:
            # Candidate has language but level is unspecified/unknown
            unknown_languages.append(f"{lang} (candidate level is unspecified; required verification)")
        elif req_score is not None and cand_score < req_score:
            unmet_languages.append(f"{lang} (requires {req_lvl_str}, candidate has {cand_lvl_str})")

    if unmet_languages:
        return CriterionDecision(
            status=CriterionStatus.UNMET,
            reason=f"Mandatory language requirement unmet: {'; '.join(unmet_languages)}.",
            evidence_spans=evidence_spans,
            checked_at=now_str,
            rule_version=LANGUAGE_RULE_VERSION,
            profile_version=prof_ver,
        )

    if unknown_languages:
        return CriterionDecision(
            status=CriterionStatus.UNKNOWN,
            reason=f"Mandatory language level could not be verified: {'; '.join(unknown_languages)}.",
            evidence_spans=evidence_spans,
            checked_at=now_str,
            rule_version=LANGUAGE_RULE_VERSION,
            profile_version=prof_ver,
        )

    return CriterionDecision(
        status=CriterionStatus.MET,
        reason="Candidate satisfies all explicit mandatory working language requirements.",
        evidence_spans=evidence_spans,
        checked_at=now_str,
        rule_version=LANGUAGE_RULE_VERSION,
        profile_version=prof_ver,
    )
