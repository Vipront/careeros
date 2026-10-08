"""Data models and contract definitions for recommendation eligibility."""

from __future__ import annotations

from enum import Enum
from typing import Any, List, Optional
from pydantic import BaseModel, Field


class CriterionStatus(str, Enum):
    MET = "met"
    UNMET = "unmet"
    UNKNOWN = "unknown"


class LivenessStatusContract(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class OverallEligibilityStatus(str, Enum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    REVIEW = "review"


class CriterionDecision(BaseModel):
    status: CriterionStatus
    reason: str
    evidence_spans: List[str] = Field(default_factory=list)
    checked_at: str
    rule_version: str = "1.0.0"
    profile_version: Optional[str] = None
    prompt_version: Optional[str] = None
    model_version: Optional[str] = None


class LivenessDecision(BaseModel):
    status: LivenessStatusContract
    reason: str
    evidence_spans: List[str] = Field(default_factory=list)
    checked_at: str
    http_code: Optional[int] = None
    rule_version: str = "1.0.0"
    is_stale: bool = False


class EligibilityDecision(BaseModel):
    """Canonical multi-criteria recommendation decision contract."""

    job_id: str
    overall_status: OverallEligibilityStatus
    can_recommend: bool
    can_generate_documents: bool
    can_notify: bool
    experience: CriterionDecision
    language: CriterionDecision
    posting_language_preference: CriterionDecision
    liveness: LivenessDecision
    hard_block_reasons: List[str] = Field(default_factory=list)
    review_reasons: List[str] = Field(default_factory=list)
    evaluated_at: str
    rule_version: str = "1.0.0"
    profile_version: str = "1.0.0"

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
