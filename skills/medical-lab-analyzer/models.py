"""Pydantic data models for lab analysis structuring and interpretation."""

from __future__ import annotations

from typing import List, Optional, Union

from pydantic import BaseModel, Field, field_validator


ALLOWED_STATUSES = frozenset(
    {"", "норма", "отклонение", "информационно", "повышен", "снижен"}
)

STATUS_EMOJI = {
    "норма": "🟢",
    "повышен": "🔴",
    "снижен": "🔵",
    "отклонение": "🟠",
    "информационно": "⚪",
    "": "⚪",
}


def _normalize_status(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normed = str(value).strip().lower()
    if normed in ALLOWED_STATUSES:
        return normed
    return ""


# ── Structuring models ────────────────────────────────────


class Reference(BaseModel):
    description: str = ""
    low_value: Optional[Union[int, float]] = None
    high_value: Optional[Union[int, float]] = None


class LabTestValue(BaseModel):
    name: str
    value: Optional[str] = None
    unit: Optional[str] = None
    biomaterial: Optional[str] = None
    reference: Optional[Reference] = None
    comment: Optional[str] = None
    status: Optional[str] = None

    @field_validator("status", mode="before")
    @classmethod
    def validate_status(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_status(v)


class PatientInfo(BaseModel):
    sex: Optional[bool] = None  # True=male, False=female
    age: Optional[int] = None
    date_of_birth: Optional[str] = None


class LabAnalysis(BaseModel):
    """Full structured result of a single lab report."""

    patient: PatientInfo = Field(default_factory=PatientInfo)
    tests: List[LabTestValue] = Field(default_factory=list)
    text_type: Optional[str] = None  # "lab" | "instrumental" | "other"
    raw_text: str = ""
    interpretation: str = ""
    recommendations: str = ""


# ── Response helpers for LLM JSON parsing ─────────────────


class TextClassificationResponse(BaseModel):
    result: int  # 1=lab, 2=instrumental, 3=other


class TestNamesResponse(BaseModel):
    tests: List[str] = Field(default_factory=list)


class SexAgeResponse(BaseModel):
    sex: Optional[bool] = None
    age: Optional[int] = None
    date_of_birth: Optional[str] = None
