"""Adapter to extract verified candidate profile facts from runtime CV/profile files.

Does not hardcode candidate identity, dates, or personal data into source code.
Absent or incomplete fields remain None (unknown).
Unions overlapping date intervals across research and industry categories.
Mixed dated and undated entries resolve to None (unknown total).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional

ROOT = Path(__file__).resolve().parents[2]


CEFR_LEVEL_MAP: dict[str, int] = {
    "a1": 1,
    "a2": 2,
    "b1": 3,
    "b2": 4,
    "c1": 5,
    "c2": 6,
    "native": 7,
    "anadil": 7,
}


def parse_cefr_level(level_str: Optional[str]) -> Optional[int]:
    """Parse text into numeric CEFR scale (1-7). Returns None if level is unspecified/unknown."""
    if not level_str:
        return None
    lvl = level_str.lower().strip()
    if lvl == "unspecified":
        return None
    for key, val in CEFR_LEVEL_MAP.items():
        if re.search(r"\b" + re.escape(key) + r"\b", lvl):
            return val
    if "fluent" in lvl or "fließend" in lvl or "courant" in lvl:
        return 5  # C1 equivalent
    if "professional working" in lvl or "working proficiency" in lvl:
        return 4  # B2 equivalent
    if "intermediate" in lvl:
        return 3  # B1 equivalent
    if "basic" in lvl or "elementary" in lvl:
        return 2  # A2 equivalent
    return None


def _parse_month_year(text: str) -> Optional[datetime]:
    text = text.strip()
    month_names = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    }
    m_my = re.match(r"([A-Za-z]+)\s+(\d{4})", text)
    if m_my:
        m_str, y_str = m_my.group(1).lower(), m_my.group(2)
        month = month_names.get(m_str)
        if month:
            return datetime(int(y_str), month, 1)
    m_y = re.match(r"^(\d{4})$", text)
    if m_y:
        return datetime(int(m_y.group(1)), 1, 1)
    return None


def _parse_interval(dates_str: str) -> Optional[tuple[datetime, datetime]]:
    if not dates_str or not isinstance(dates_str, str):
        return None
    normalized = dates_str.replace("–", "-").replace("—", "-")
    parts = normalized.split("-")
    if len(parts) == 2:
        dt1 = _parse_month_year(parts[0])
        dt2 = _parse_month_year(parts[1])
        if dt1 and dt2 and dt2 >= dt1:
            end_dt = datetime(dt2.year, dt2.month, 28)
            return (dt1, end_dt)
    return None


def _union_intervals_duration_years(intervals: list[tuple[datetime, datetime]]) -> float:
    """Merge overlapping datetime intervals and return total duration in years."""
    if not intervals:
        return 0.0
    sorted_intervals = sorted(intervals, key=lambda x: x[0])
    merged: list[tuple[datetime, datetime]] = [sorted_intervals[0]]

    for current in sorted_intervals[1:]:
        prev_start, prev_end = merged[-1]
        if current[0] <= prev_end:
            merged[-1] = (prev_start, max(prev_end, current[1]))
        else:
            merged.append(current)

    total_days = sum((end - start).days + 1 for start, end in merged)
    return round(total_days / 365.25, 2)


def compute_profile_version(facts: Mapping[str, Any]) -> str:
    """Generate deterministic version hash from canonical verified profile facts."""
    canonical_dict = dict(facts)
    serialized = json.dumps(canonical_dict, sort_keys=True, ensure_ascii=True, default=str)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return f"prof-{digest[:12]}"


def get_candidate_profile_facts(profile_data: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Extract verified factual metrics from master profile without inventing facts.

    Absent fields remain None. Overlapping experience intervals are unioned across categories.
    Mixed dated and undated entries resolve to None (unknown total).
    Bare languages remain unspecified.
    """
    if profile_data is None:
        p = ROOT / "data" / "master_cv.json"
        if p.exists():
            try:
                profile_data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                profile_data = {}
        else:
            profile_data = {}

    education = profile_data.get("education")
    completed_degrees: list[str] = []
    enrolled_programs: list[str] = []
    is_graduated: Optional[bool] = None

    if education is not None:
        if not education:
            is_graduated = None
        else:
            has_in_progress = False
            has_completed = False
            current_year = 2026
            for edu in education:
                deg = edu.get("degree", "")
                dates = edu.get("dates", "")
                dates_lower = dates.lower()
                is_future = False
                year_match = re.search(r"\b(202[7-9]|20[3-9]\d)\b", dates)
                if year_match and int(year_match.group(1)) > current_year:
                    is_future = True

                if "expected" in dates_lower or "in progress" in dates_lower or is_future:
                    enrolled_programs.append(deg)
                    has_in_progress = True
                elif dates.strip():
                    completed_degrees.append(deg)
                    has_completed = True
                else:
                    enrolled_programs.append(deg)

            if has_completed and not has_in_progress:
                is_graduated = True
            elif has_in_progress:
                is_graduated = False
            else:
                is_graduated = None

    # Parse research experience
    research_list = profile_data.get("research_experience")
    research_years: Optional[float] = None
    research_intervals: list[tuple[datetime, datetime]] = []
    research_has_undated = False

    if research_list is not None:
        if not research_list:
            research_years = 0.0
        else:
            for exp in research_list:
                dates = exp.get("dates", "")
                interval = _parse_interval(dates)
                if interval:
                    research_intervals.append(interval)
                else:
                    research_has_undated = True
            if research_has_undated:
                research_years = None
            else:
                research_years = _union_intervals_duration_years(research_intervals)

    # Parse industry / work experience
    industry_list = profile_data.get("work_experience", profile_data.get("industry_experience"))
    industry_years: Optional[float] = None
    industry_intervals: list[tuple[datetime, datetime]] = []
    industry_has_undated = False

    if industry_list is not None:
        if not industry_list:
            industry_years = 0.0
        else:
            for exp in industry_list:
                dates = exp.get("dates", "")
                interval = _parse_interval(dates)
                if interval:
                    industry_intervals.append(interval)
                else:
                    industry_has_undated = True
            if industry_has_undated:
                industry_years = None
            else:
                industry_years = _union_intervals_duration_years(industry_intervals)

    # Cross-category union for total experience
    total_general: Optional[float] = None
    if research_has_undated or industry_has_undated:
        total_general = None
    elif research_list is not None or industry_list is not None:
        all_intervals = research_intervals + industry_intervals
        if all_intervals:
            total_general = _union_intervals_duration_years(all_intervals)
        elif research_list == [] and industry_list == []:
            total_general = 0.0

    # Parse languages
    skills = profile_data.get("skills", {})
    lang_list = skills.get("languages")
    languages: Optional[dict[str, str]] = None
    if lang_list is not None:
        languages = {}
        for item in lang_list:
            if isinstance(item, str):
                m = re.match(r"^([A-Za-z]+)\s*\((.*?)\)$", item.strip())
                if m:
                    languages[m.group(1).strip()] = m.group(2).strip()
                else:
                    languages[item.strip()] = "unspecified"

    facts: dict[str, Any] = {
        "is_graduated": is_graduated,
        "completed_degrees": completed_degrees,
        "enrolled_programs": enrolled_programs,
        "research_experience_years": research_years,
        "industry_experience_years": industry_years,
        "total_work_experience_years": total_general,
        "languages": languages,
    }
    facts["profile_version"] = compute_profile_version(facts)
    return facts
