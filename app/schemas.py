"""Pydantic request/response schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict

from .models import (
    CompanyType,
    LostReason,
    PersonRole,
    Rating,
    Stage,
)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- Company ---
class CompanyBase(BaseModel):
    name: str
    company_type: CompanyType = CompanyType.EMPLOYER
    website: Optional[str] = None
    industry: Optional[str] = None
    notes: Optional[str] = None


class CompanyCreate(CompanyBase):
    pass


class CompanyUpdate(BaseModel):
    name: Optional[str] = None
    company_type: Optional[CompanyType] = None
    website: Optional[str] = None
    industry: Optional[str] = None
    notes: Optional[str] = None


class CompanyRead(ORMModel, CompanyBase):
    id: int
    created_at: Optional[datetime] = None


# --- Job Posting ---
class JobPostingBase(BaseModel):
    company_id: int
    title: str
    url: Optional[str] = None
    jd_text: Optional[str] = None
    location: Optional[str] = None
    comp_min: Optional[float] = None
    comp_max: Optional[float] = None
    comp_currency: Optional[str] = "USD"
    posted_date: Optional[datetime] = None


class JobPostingCreate(JobPostingBase):
    pass


class JobPostingRead(ORMModel, JobPostingBase):
    id: int
    dedup_key: Optional[str] = None
    my_rating: Optional[Rating] = None
    rating_reason: Optional[str] = None
    rated_at: Optional[datetime] = None
    first_seen_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None


class RatePosting(BaseModel):
    rating: Rating
    reason: Optional[str] = None


# --- Resume ---
class ResumeBase(BaseModel):
    label: str
    content: Optional[str] = None
    notes: Optional[str] = None


class ResumeCreate(ResumeBase):
    pass


class ResumeRead(ORMModel, ResumeBase):
    id: int
    created_at: Optional[datetime] = None


# --- Job Application ---
class JobApplicationBase(BaseModel):
    company_id: int
    job_posting_id: Optional[int] = None
    resume_id: Optional[int] = None
    title: Optional[str] = None
    stage: Stage = Stage.SAVED
    applied_date: Optional[datetime] = None
    notes: Optional[str] = None


class JobApplicationCreate(JobApplicationBase):
    pass


class JobApplicationRead(ORMModel, JobApplicationBase):
    id: int
    lost_reason: Optional[LostReason] = None
    last_activity_date: Optional[datetime] = None
    created_at: Optional[datetime] = None


class ChangeStage(BaseModel):
    stage: Stage
    lost_reason: Optional[LostReason] = None


class StageHistoryRead(ORMModel):
    id: int
    from_stage: Optional[Stage] = None
    to_stage: Stage
    changed_at: datetime


# --- Person ---
class PersonBase(BaseModel):
    company_id: int
    application_id: Optional[int] = None
    name: str
    role: PersonRole = PersonRole.RECRUITER
    email: Optional[str] = None
    phone: Optional[str] = None
    linkedin: Optional[str] = None
    is_champion: int = 0
    notes: Optional[str] = None


class PersonCreate(PersonBase):
    pass


class PersonRead(ORMModel, PersonBase):
    id: int
