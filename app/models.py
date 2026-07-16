"""SQLAlchemy models for the Job Search CRM.

The relationships encode the Salesforce analogy documented in ARCHITECTURE.md:

  * master-detail  -> NOT NULL FK + ON DELETE CASCADE
  * lookup         -> nullable FK + ON DELETE SET NULL

Company (Account) is the master of Job Posting, Job Application, and Person.
Stage History is the master-detail child of Job Application and is written
automatically whenever an application's stage changes.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    event,
)
from sqlalchemy.orm import Session, relationship

from .database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Enums (picklists)
# --------------------------------------------------------------------------- #
class CompanyType(str, enum.Enum):
    EMPLOYER = "Employer"
    AGENCY = "Agency"
    BOTH = "Both"


class Rating(str, enum.Enum):
    UP = "Up"
    DOWN = "Down"
    NEUTRAL = "Neutral"


class Stage(str, enum.Enum):
    SAVED = "Saved"
    APPLIED = "Applied"
    RECRUITER_SCREEN = "Recruiter Screen"
    HIRING_MANAGER_SCREEN = "Hiring Manager Screen"
    ONSITE = "Onsite / Technical"
    OFFER = "Offer"
    CLOSED_WON = "Closed Won"
    CLOSED_LOST = "Closed Lost"


# Ordered list used for funnel/conversion analysis and UI ordering.
STAGE_ORDER = [
    Stage.SAVED,
    Stage.APPLIED,
    Stage.RECRUITER_SCREEN,
    Stage.HIRING_MANAGER_SCREEN,
    Stage.ONSITE,
    Stage.OFFER,
    Stage.CLOSED_WON,
]

CLOSED_STAGES = {Stage.CLOSED_WON, Stage.CLOSED_LOST}


class LostReason(str, enum.Enum):
    GHOSTED = "Ghosted"
    REJECTED_AFTER_APPLICATION = "Rejected after application"
    REJECTED_AFTER_SCREEN = "Rejected after screen"
    REJECTED_AFTER_ONSITE = "Rejected after onsite"
    DECLINED_BY_ME = "Declined by me"
    ROLE_CLOSED = "Role closed / paused"
    OTHER = "Other"


class PersonRole(str, enum.Enum):
    RECRUITER = "Recruiter"
    HIRING_MANAGER = "Hiring Manager"
    INTERVIEWER = "Interviewer"
    REFERRAL = "Referral"
    OTHER = "Other"


# --------------------------------------------------------------------------- #
# Company  (== Account)
# --------------------------------------------------------------------------- #
class Company(Base):
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False, index=True)
    company_type = Column(Enum(CompanyType), nullable=False, default=CompanyType.EMPLOYER)
    website = Column(String(512))
    industry = Column(String(255))
    notes = Column(Text)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    # Master-detail children: deleting a company cascades to these.
    postings = relationship(
        "JobPosting", back_populates="company", cascade="all, delete-orphan"
    )
    applications = relationship(
        "JobApplication", back_populates="company", cascade="all, delete-orphan"
    )
    people = relationship(
        "Person", back_populates="company", cascade="all, delete-orphan"
    )


# --------------------------------------------------------------------------- #
# Job Posting  (== Product): catalog data, exists whether or not I apply
# --------------------------------------------------------------------------- #
class JobPosting(Base):
    __tablename__ = "job_postings"

    id = Column(Integer, primary_key=True)

    # Master-detail to Company: required + cascade.
    company_id = Column(
        Integer, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )

    title = Column(String(512), nullable=False)
    url = Column(String(1024))
    # Dedup: canonical id parsed from the ATS URL when available (indexed).
    dedup_key = Column(String(512), index=True)
    # When a posting is seen on multiple boards, extra URLs accumulate here.
    source_urls = Column(Text)  # JSON-encoded list[str]

    jd_text = Column(Text)
    location = Column(String(255))
    comp_min = Column(Float)
    comp_max = Column(Float)
    comp_currency = Column(String(8), default="USD")

    posted_date = Column(DateTime)
    first_seen_at = Column(DateTime, default=_utcnow)
    last_seen_at = Column(DateTime, default=_utcnow)

    # Sourcing feedback loop (kept directly on the posting for v1).
    my_rating = Column(Enum(Rating))
    rating_reason = Column(Text)
    rated_at = Column(DateTime)

    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    company = relationship("Company", back_populates="postings")
    # An application optionally points here (lookup); don't cascade-delete apps.
    applications = relationship("JobApplication", back_populates="job_posting")


# --------------------------------------------------------------------------- #
# Resume: versioned asset referenced (optionally) by an application
# --------------------------------------------------------------------------- #
class Resume(Base):
    __tablename__ = "resumes"

    id = Column(Integer, primary_key=True)
    label = Column(String(255), nullable=False)  # e.g. "RevOps v3 - metrics-forward"
    content = Column(Text)  # extracted plain text (what analysis runs on)
    source_link = Column(String(1024))  # optional reference, e.g. a Google Drive URL
    filename = Column(String(512))  # original uploaded filename, if any
    notes = Column(Text)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    applications = relationship("JobApplication", back_populates="resume")


# --------------------------------------------------------------------------- #
# Job Application  (== Opportunity): pipeline data, exists because I applied
# --------------------------------------------------------------------------- #
class JobApplication(Base):
    __tablename__ = "job_applications"

    id = Column(Integer, primary_key=True)

    # Master-detail to Company: required + cascade. This is the EMPLOYER, and is
    # kept independent of whichever posting/agency sourced the role (see docs).
    company_id = Column(
        Integer, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Lookup to the posting I applied to (nullable: cold outreach has none).
    job_posting_id = Column(
        Integer, ForeignKey("job_postings.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Lookup to the resume version used (nullable).
    resume_id = Column(
        Integer, ForeignKey("resumes.id", ondelete="SET NULL"), nullable=True, index=True
    )

    stage = Column(Enum(Stage), nullable=False, default=Stage.SAVED, index=True)
    lost_reason = Column(Enum(LostReason))  # only meaningful when Closed Lost

    title = Column(String(512))  # denormalized role title for convenience
    applied_date = Column(DateTime)
    last_activity_date = Column(DateTime, default=_utcnow)
    notes = Column(Text)

    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    company = relationship("Company", back_populates="applications")
    job_posting = relationship("JobPosting", back_populates="applications")
    resume = relationship("Resume", back_populates="applications")

    # Master-detail child: cascade delete the funnel history with the app.
    stage_history = relationship(
        "StageHistory",
        back_populates="application",
        cascade="all, delete-orphan",
        order_by="StageHistory.changed_at",
    )
    # People tied to this application (lookup from Person side).
    people = relationship("Person", back_populates="application")
    # Meetings (interviews / calls) for this application (master-detail).
    meetings = relationship(
        "Meeting",
        back_populates="application",
        cascade="all, delete-orphan",
        order_by="Meeting.meeting_date",
    )


# --------------------------------------------------------------------------- #
# Stage History  (== OpportunityFieldHistory): append-only funnel log
# --------------------------------------------------------------------------- #
class StageHistory(Base):
    __tablename__ = "stage_history"

    id = Column(Integer, primary_key=True)
    application_id = Column(
        Integer,
        ForeignKey("job_applications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    from_stage = Column(Enum(Stage))  # null on the very first (creation) row
    to_stage = Column(Enum(Stage), nullable=False)
    changed_at = Column(DateTime, default=_utcnow, index=True)

    application = relationship("JobApplication", back_populates="stage_history")


# --------------------------------------------------------------------------- #
# Person  (== Contact)
# --------------------------------------------------------------------------- #
class Person(Base):
    __tablename__ = "people"

    id = Column(Integer, primary_key=True)

    # Master-detail to Company: the person's OWN employer (may be an agency).
    # Deliberately independent of the application's company.
    company_id = Column(
        Integer, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Lookup to an application (nullable).
    application_id = Column(
        Integer,
        ForeignKey("job_applications.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    name = Column(String(255), nullable=False)
    role = Column(Enum(PersonRole), default=PersonRole.RECRUITER)
    email = Column(String(255))
    phone = Column(String(64))
    linkedin = Column(String(512))
    is_champion = Column(Integer, default=0)  # simple 0/1 flag for now
    notes = Column(Text)

    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    company = relationship("Company", back_populates="people")
    application = relationship("JobApplication", back_populates="people")


# --------------------------------------------------------------------------- #
# Meeting  (== Activity/Event on an Opportunity): an interview or call
# --------------------------------------------------------------------------- #
class MeetingType(str, enum.Enum):
    RECRUITER_SCREEN = "Recruiter Screen"
    HIRING_MANAGER = "Hiring Manager"
    TECHNICAL = "Technical"
    ONSITE = "Onsite"
    PANEL = "Panel"
    OTHER = "Other"


class Meeting(Base):
    __tablename__ = "meetings"

    id = Column(Integer, primary_key=True)

    # Master-detail to Application: a meeting exists because you're pursuing a
    # role. Through the application it also reaches the Posting (JD) and Resume —
    # which is what makes "questions by JD / by resume" analysis possible.
    application_id = Column(
        Integer,
        ForeignKey("job_applications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    title = Column(String(512))
    meeting_type = Column(Enum(MeetingType))
    meeting_date = Column(DateTime)

    summary = Column(Text)      # AI summary (e.g. from Granola)
    transcript = Column(Text)   # full transcript — where the questions live
    notes = Column(Text)        # your own notes

    granola_note_id = Column(String(255), index=True)  # for linking / de-dup
    granola_link = Column(String(1024))

    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    application = relationship("JobApplication", back_populates="meetings")


# --------------------------------------------------------------------------- #
# Auto stage-history: record every change to JobApplication.stage
# --------------------------------------------------------------------------- #
@event.listens_for(JobApplication.stage, "set", active_history=True)
def _record_stage_change(target: JobApplication, value, oldvalue, initiator):
    """Queue a StageHistory row whenever `stage` is assigned to a new value.

    Uses active_history so `oldvalue` is reliably populated. The history row is
    appended to the relationship so it participates in the same unit-of-work
    flush as the stage change itself. On the first set (object creation) the
    old value is a SQLAlchemy sentinel that is not a real Stage member.
    """
    if value == oldvalue:
        return
    # Treat anything that isn't an actual Stage (sentinels like NO_VALUE, or
    # None) as "no previous stage" -> from_stage stays NULL on the opening row.
    old = oldvalue if isinstance(oldvalue, Stage) else None
    target.stage_history.append(
        StageHistory(from_stage=old, to_stage=value, changed_at=_utcnow())
    )
