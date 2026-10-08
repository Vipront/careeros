import re
from datetime import datetime, timezone
from pathlib import Path

from src.db import get_connection, transition, utc_now as now

ROOT = Path(__file__).resolve().parents[2]



TITLE_HARD_RULES = [
    (re.compile(r"\bsenior\b|\bkıdemli\b|\bkidemli\b", re.I), "Senior position"),
    (
        re.compile(r"\b(?:principal|director|head|lead)\b", re.I),
        "Principal/leadership position",
    ),
    (
        re.compile(r"\bpost\s*doc(?:toral)?\b|\bpost-doctoral\b", re.I),
        "Postdoc position",
    ),
    (
        re.compile(
            r"\bph\s*\.?\s*d\s*\.?\s+(?:student|candidate)\b",
            re.I,
        ),
        "PhD position",
    ),
    (
        re.compile(r"\bphd\s+(?:student|candidate)\b", re.I),
        "PhD position",
    ),
    (
        re.compile(r"\bph\.?d\.?\s+(?:scientist|chemist|biologist|researcher|engineer|analyst|fellow|investigator)\b", re.I),
        "PhD-level required role",
    ),
    (
        re.compile(r"\b(?:scientist|chemist|biologist|researcher)\s*-\s*ph\.?d\.?\b", re.I),
        "PhD-level required role",
    ),
    (
        re.compile(r"^(?:ph\.?d\.?|phd)\s+", re.I),
        "PhD-level role",
    ),
]

DESCRIPTION_HARD_RULES = [
    (
        re.compile(
            r"\bph\s*\.?\s*d\s*\.?\s+(?:is\s+)?(?:mandatory|required)\b",
            re.I,
        ),
        "PhD mandatory",
    ),
    (
        re.compile(
            r"\bphd\s+(?:is\s+)?(?:mandatory|required)\b",
            re.I,
        ),
        "PhD mandatory",
    ),
    (
        re.compile(
            r"\bmd\s+(?:is\s+)?(?:mandatory|required)\b",
            re.I,
        ),
        "MD required",
    ),
    (
        re.compile(
            r"\beu\s+citizenship\b.*\b(?:mandatory|required)\b",
            re.I,
        ),
        "EU citizenship required",
    ),
]

REVIEW_TITLE_RULES = [
    (
        re.compile(r"\bspecialist\b", re.I),
        "Specialist role — experience level review",
    ),
    (
        re.compile(r"\bexpert\b", re.I),
        "Expert role — experience level review",
    ),
    (
        re.compile(r"\bmanager\b", re.I),
        "Manager role — experience level review",
    ),
]


def role_level_flag(title):
    for pattern, flag in REVIEW_TITLE_RULES:
        if pattern.search(title or ""):
            return flag
    return None


def normalize_degree_text(text):
    return (
        (text or "")
        .replace("’", "'")
        .replace("‘", "'")
        .replace("–", "-")
        .replace("—", "-")
        .replace("\u00a0", " ")
    )


def has_degree_alternative(text):
    """
    True for:
      PhD or Master's degree
      Ph.D. or Master’s degree
      PhD or equivalent
      PhD or Bachelor's degree
    """

    t = re.sub(r"\s+", " ", normalize_degree_text(text).lower())

    has_doctoral = bool(
        re.search(
            r"\bph\s*\.?\s*d\s*\.?\b|\bphd\b|\bdoctoral\b|\bdoctorate\b",
            t,
            re.I,
        )
    )

    if not has_doctoral:
        return False

    has_master = bool(
        re.search(
            r"\bmaster'?s?\b|\bm\.?\s*sc\.?\b",
            t,
            re.I,
        )
    )

    has_bachelor = bool(
        re.search(
            r"\bbachelor'?s?\b|\bb\.?\s*sc\.?\b",
            t,
            re.I,
        )
    )

    has_equivalent = bool(
        re.search(r"\bequivalent\b", t, re.I)
    )

    has_or = bool(
        re.search(r"\bor\b", t, re.I)
    )

    if has_equivalent:
        return True

    if has_or and (has_master or has_bachelor):
        return True

    return False


def knockout_reason(
    title,
    description="",
    education_requirements="",
    experience_requirements="",
    eligibility_text="",
    date_found="",
    date_posted="",
):
    title = title or ""
    description = description or ""

    education_requirements = normalize_degree_text(
        education_requirements
    )
    experience_requirements = normalize_degree_text(
        experience_requirements
    )
    eligibility_text = normalize_degree_text(
        eligibility_text
    )

    # 1. Title-based hard knockout
    for pattern, reason in TITLE_HARD_RULES:
        if pattern.search(title):
            return reason

    # 2. Description-based hard knockout
    for pattern, reason in DESCRIPTION_HARD_RULES:
        if pattern.search(description):
            return reason

    enriched = (
        education_requirements
        + "\n"
        + eligibility_text
    ).strip()

    # 3. Doctoral requirement
    if re.search(
        r"\bph\s*\.?\s*d\s*\.?\b"
        r"|\bphd\b"
        r"|\bdoctorate\b"
        r"|\bdoctoral\b"
        r"|\bpostdoctoral\b"
        r"|\bpostdoc\b",
        enriched,
        re.I,
    ):
        # Only reject when there is NO accepted alternative.
        if not has_degree_alternative(enriched):
            return "PhD/doctoral requirement"

    # 4. Evidence-based experience and education eligibility evaluation
    from src.eligibility.experience import evaluate_experience_eligibility
    from src.eligibility.models import CriterionStatus

    exp_decision = evaluate_experience_eligibility(
        title=title,
        description=description,
        experience_requirements=experience_requirements,
        education_requirements=education_requirements,
    )
    if exp_decision.status == CriterionStatus.UNMET:
        return f"Experience: {exp_decision.reason}"

    # 6. 7-Day Age Check (İlan Yayınlanma/Bulunma Tarihi 7 Günden Eski Olamaz)
    age_reason = check_job_age_knockout(date_found, date_posted, description)
    if age_reason:
        return age_reason

    return None


def check_job_age_knockout(date_found_str, date_posted_str="", description=""):
    desc = (description or "").lower()
    old_patterns = [
        r"\bposted\s+(?:2|3|4|[5-9]|\d{2,})\s+weeks?\s+ago\b",
        r"\bposted\s+(?:[1-9]|\d{2,})\s+months?\s+ago\b",
        r"\bposted\s+30\+\s+days\s+ago\b",
        r"\b(?:2|3|4|[5-9]|\d{2,})\s+hafta\s+önce\s+yayınlandı\b",
        r"\b(?:[1-9]|\d{2,})\s+ay\s+önce\s+yayınlandı\b",
        r"\bvor\s+(?:2|3|4|[5-9]|\d{2,})\s+Wochen\s+veröffentlicht\b",
        r"\bvor\s+(?:[1-9]|\d{2,})\s+Monat(?:en)?\s+veröffentlicht\b",
    ]
    for pat in old_patterns:
        if re.search(pat, desc, re.I):
            return "İlan 7 günden eski (Açıklamada eski tarihli olduğu belirtilmiş)"

    date_val = date_posted_str or date_found_str
    if date_val:
        try:
            dt = datetime.fromisoformat(date_val.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
            if age_days > 7.0:
                return f"İlan 7 günden eski ({int(age_days)} gün önce açılmış/bulunmuş)"
        except (ValueError, TypeError):
            # Missing or malformed dates cannot establish an age cutoff.
            return None

    return None


def run():
    con = get_connection()
    con.execute("PRAGMA foreign_keys=ON")

    rows = con.execute(
        """
        SELECT
            id,
            title,
            description,
            education_requirements,
            experience_requirements,
            eligibility_text,
            date_found,
            date_posted
        FROM jobs
        WHERE status IN ('new', 'evaluated')
        """
    ).fetchall()

    timestamp = now()

    for (
        job_id,
        title,
        description,
        education,
        experience,
        eligibility,
        date_found,
        date_posted,
    ) in rows:

        reason = knockout_reason(
            title,
            description,
            education,
            experience,
            eligibility,
            date_found=date_found,
            date_posted=date_posted,
        )

        flag = role_level_flag(title)

        if reason:
            transition(job_id, "rejected", note=f"Knockout: {reason}", connection=con, system=True)
            con.execute(
                """
                UPDATE jobs
                SET
                    knockout_reason=?,
                    updated_at=?
                WHERE id=?
                """,
                (reason, timestamp, job_id),
            )

            note = reason

        else:
            transition(job_id, "evaluated", note=flag or "Knockout passed", connection=con, system=True)
            con.execute(
                """
                UPDATE jobs
                SET
                    knockout_reason=NULL,
                    updated_at=?
                WHERE id=?
                """,
                (timestamp, job_id),
            )

            note = flag or "passed"

        con.execute(
            """
            INSERT INTO events(
                job_id,
                event_type,
                event_time,
                note
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                job_id,
                "knockout_filter",
                timestamp,
                note,
            ),
        )

    con.commit()
    con.close()


if __name__ == "__main__":
    run()
