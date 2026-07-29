"""SQLAlchemy models for the Job Search CRM.

The relationships encode the Salesforce analogy documented in ARCHITECTURE.md:

  * master-detail  -> NOT NULL FK + ON DELETE CASCADE
  * lookup         -> nullable FK + ON DELETE SET NULL
  * many-to-many   -> join table, ON DELETE CASCADE on both FKs (removes the
                      association row on either side's deletion, never the
                      other side's actual row)

Company (Account) is the master of Job Posting, Job Application, and Person.
Stage History is the master-detail child of Job Application and is written
automatically whenever an application's stage changes. Email Thread relates
to Person through a many-to-many join table rather than a master-detail FK
(see EmailThread's docstring below for why).

Scoring
-------
Meeting and Email Thread each carry a nullable `score` (0-100), a
`score_reason`, and a `scored_at`. The score answers one question: *given
what just happened in this interaction, how likely is this application to
end up Closed Won?*

The score lives on the **activity**, not on the Application, on purpose. A
single number on the Application would only ever tell you where you stand
right now; a reading attached to each interaction turns the same data into a
time series, so you can see a pursuit gaining or losing momentum (55 -> 70
-> 30 after an interview that went badly) rather than just its latest value.
The Application derives "current score + trend" from these at display time —
no stored column, so there's nothing to keep in sync and no chance of the
rollup disagreeing with the rows it came from.

Two deliberate consequences:

  * `scored_at` is separate from the activity's own date because you might
    score a meeting days after it happened, and calibration analysis later
    needs to know when you formed the judgment, not just when you talked.
  * Nothing writes `score` automatically today — you enter it. The three
    columns are shaped so an automated scorer (an LLM reading the transcript
    or thread body) can populate exactly the same fields later without a
    schema change or a backfill; `score_reason` is where its rationale would
    go, the same place yours does.

Because every scored activity hangs off an application that eventually
reaches a terminal stage, these scores accumulate into labeled training data
for free: "when I scored 70 after a hiring-manager call, how often did that
actually close?" is answerable from `score` + `Stage`, and is the reason the
reason-text is captured rather than the bare number.

A Meeting additionally carries `my_performance` and `employer_engagement`,
which decompose the blended `score` into its two independent causes: how well
you did, and how interested they are. They are separate columns rather than a
single "quality" number because they move independently and the difference is
the actionable part. A great performance met with flat engagement means the
role is probably going elsewhere and you should stop spending here; a
mediocre performance met with high engagement means you're still in it and
the fix is yours to make. Averaged into one number, both read as "medium" and
tell you nothing. All three stay nullable and independent -- scoring one does
not require scoring the others, and blank continues to mean "no judgment
formed" rather than zero.

Forecast
--------
An Application carries a `manual_forecast` you maintain by hand. Its
automated counterpart is *not* a column: it's derived at display time in
app/forecast.py from stage, source, meeting quality, and resume/JD fit, for
the same reason the score rollup isn't stored -- a stored forecast is a
snapshot that silently goes stale the moment any input under it moves. Two
independent reads that can visibly disagree is the point; a disagreement is
information, and it vanishes if the machine can overwrite your column.
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
    Table,
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
    """Macro, decision-oriented stages -- modeled on the seller's own
    qualification/pursuit logic (like a sales opportunity), not a literal
    mirror of any one employer's interview loop. That's deliberate: every
    employer's loop is shaped differently (some skip a recruiter screen,
    some do five rounds), so a stage model built around "which call is this"
    doesn't generalize -- one built around your own evaluation of the
    opportunity does. See ARCHITECTURE.md / the July 2026 stage redesign.
    """

    STAGING = "Staging"                 # pre-application: working the angle in
    QUALIFICATION = "Qualification"     # is this worth pursuing at all
    DISCOVERY = "Discovery"             # how strong a fit, both directions
    TAKEHOME = "Takehome"               # proof you can do the work
    EXECUTIVE_SIGNOFF = "Executive Signoff"  # final internal approval, seen or not
    NEGOTIATION = "Negotiation"         # offer is on the table
    CLOSED_WON = "Closed Won"
    CLOSED_LOST = "Closed Lost"


# Ordered list used for funnel/conversion analysis and UI ordering.
#
# Two members of Stage are deliberately absent: Closed Lost (a terminal exit,
# not a depth) and Staging (see below). Everything that walks this list --
# funnel counts, conversion, resume traction -- therefore ignores both.
#
# Staging is where a role sits before you've applied to it: you found the
# posting and you're working the angle in (finding the referral, warming the
# intro). It's a real state you occupy and do work in, which is why it's a
# stage and not a date column -- but it is *not* a rung of the funnel, and the
# reason is the column default. `stage` defaults to QUALIFICATION, so every
# application is born there; combined with the prefix-crediting in
# analytics._reached_sets, whichever stage is the default is reached by 100%
# of applications by construction and is worthless as a denominator. Putting
# Staging at the front of STAGE_ORDER would simply relocate that dead
# denominator onto Staging -- the same objection that keeps `Applied` from
# being a stage at all (see analytics.applied_conversion's docstring).
#
# Leaving it out costs nothing real. An application at Staging hasn't entered
# the pipeline yet and correctly counts toward no stage. When it does, the
# move writes a `Staging -> Qualification` StageHistory row, and *that* row is
# the honest cohort filter for "of the roles I staged, how many converted" --
# real dated transitions rather than credit implied by position in a list.
STAGE_ORDER = [
    Stage.QUALIFICATION,
    Stage.DISCOVERY,
    Stage.TAKEHOME,
    Stage.EXECUTIVE_SIGNOFF,
    Stage.NEGOTIATION,
    Stage.CLOSED_WON,
]

CLOSED_STAGES = {Stage.CLOSED_WON, Stage.CLOSED_LOST}

# The stage an application is born at when nothing else is specified. Named
# rather than inlined because the funnel's meaning depends on which stage this
# is -- see the note above STAGE_ORDER.
DEFAULT_STAGE = Stage.QUALIFICATION


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


class ApplicationSource(str, enum.Enum):
    """How this application originated.

    Deliberately about *origin*, not about who a person is: a referral from a
    friend and an inbound note from that friend's in-house recruiter are two
    different sources even if both eventually route through the same recruiter.
    Nullable on the model, because applications created before this field
    existed genuinely don't know their own answer -- better an honest blank
    than a defaulted guess that pollutes any later "which source converts
    best" analysis.
    """

    REFERRAL = "Referral"
    RECRUITER_INBOUND = "Recruiter Inbound"
    OUTBOUND = "Outbound"


class ForecastCategory(str, enum.Enum):
    """How likely this application is to end in an offer you accept.

    Borrowed straight from Salesforce forecast categories, and used by both
    the hand-maintained `manual_forecast` column and the derived Automated
    Forecast in app/forecast.py, so the two are always in the same units and
    can be read side by side.

      * Commit    -- more likely than not. Roughly 75%+.
      * Best Case -- possible if a few things break your way.
      * Pipeline  -- no signal; not forecastable. This is the residual bucket
                     and deliberately absorbs two different situations: "there
                     isn't enough evidence yet" and "there is evidence and
                     it's weak." Salesforce's separate `Omitted` category
                     lives in here too -- a role you're keeping warm but have
                     written off is, for forecasting purposes, exactly a role
                     you aren't counting.
      * Closed    -- decided, win or lose. Only selectable by hand; the
                     Automated Forecast never emits it, because Stage already
                     carries Closed Won / Closed Lost and a second source of
                     truth for "is this over" is a bug waiting to happen.
    """

    PIPELINE = "Pipeline"
    BEST_CASE = "Best Case"
    COMMIT = "Commit"
    CLOSED = "Closed"


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
    label = Column(String(255), nullable=False)  # e.g. "v3 - metrics-forward"
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

    stage = Column(Enum(Stage), nullable=False, default=DEFAULT_STAGE, index=True)
    lost_reason = Column(Enum(LostReason))  # only meaningful when Closed Lost

    title = Column(String(512))  # denormalized role title for convenience
    applied_date = Column(DateTime)
    last_activity_date = Column(DateTime, default=_utcnow)

    # How the application originated (see ApplicationSource). Nullable.
    source = Column(Enum(ApplicationSource), index=True)

    # Your own call on where this lands (see ForecastCategory). Defaults to
    # Pipeline, which is honest here in a way a default stage never was: the
    # category literally means "no signal / not forecastable", so a record
    # born there is making a true statement about itself rather than a
    # flattering guess. The cost is that this column can't distinguish "I
    # looked and judged it Pipeline" from "I never touched it" -- if that
    # distinction starts mattering, the fix is a `manual_forecast_set_at`
    # timestamp, not a nullable default.
    manual_forecast = Column(
        Enum(ForecastCategory), default=ForecastCategory.PIPELINE, index=True
    )

    # Standing context on the opportunity itself: why this role is worth
    # pursuing, what you know about the team/comp/timeline, what would make you
    # walk. Deliberately separate from `notes`:
    #   * `context` is durable input you'd re-read when judging viability, and
    #     is the field a viability assessment reads from.
    #   * `notes` stays a running scratchpad of whatever happened lately.
    # Keeping them apart means a viability check has a stable thing to read
    # instead of having to sift a chronological log.
    context = Column(Text)

    notes = Column(Text)

    # --- Generated brief -----------------------------------------------------
    # The one part of this app whose content came from a model rather than from
    # you. Stored rather than computed on the fly, which is the opposite of the
    # choice made for the automated forecast a few fields up -- and the reason
    # is cost, not principle. Deriving the forecast is arithmetic over rows
    # already in memory; deriving this is a paid API call over several
    # transcripts, so recomputing it on every page load would bill you
    # repeatedly to regenerate identical prose.
    #
    # The tradeoff that buys is the one the forecast comment warns about: a
    # stored brief goes stale the moment a meeting is added, and a stale brief
    # looks exactly like a fresh one. `brief_generated_at` is what keeps that
    # honest -- the panel dates the brief and says plainly when activity has
    # landed since, rather than presenting old prose as current.
    brief = Column(Text)
    brief_generated_at = Column(DateTime)
    # Which model wrote it. Provenance worth keeping: briefs written months
    # apart by different models are different artifacts, and when one reads
    # oddly the first useful question is what produced it.
    brief_model = Column(String(128))

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
    # Email threads optionally tied to this application (lookup from the
    # EmailThread side — a thread's people are a many-to-many, and Application
    # is only ever an optional lookup on it, so this doesn't cascade;
    # deleting the application just clears the link).
    email_threads = relationship(
        "EmailThread",
        back_populates="application",
        order_by="EmailThread.last_message_at",
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
    # The dedup key for auto-creating People from an email thread's sender
    # (see _find_or_create_person_by_email in app/routers/ui.py) -- looked up
    # case-insensitively, never enforced unique at the DB level (SQLite
    # ALTER TABLE can't add a unique constraint after the fact, and this app
    # has no migration framework), so it's an application-level guarantee.
    email = Column(String(255), index=True)
    phone = Column(String(64))
    linkedin = Column(String(512))
    is_champion = Column(Integer, default=0)  # simple 0/1 flag for now
    notes = Column(Text)

    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    company = relationship("Company", back_populates="people")
    application = relationship("JobApplication", back_populates="people")
    # Many-to-many: a thread can involve more than one person (an intro
    # thread, a BCC'd hiring manager), and a person can be on more than one
    # thread. No "primary" -- deleting a person just unlinks them from any
    # threads they're on (removes their row in the join table below); it
    # never deletes the thread itself, since other people may still be on
    # it. See ARCHITECTURE.md.
    email_threads = relationship(
        "EmailThread",
        secondary="email_thread_people",
        back_populates="people",
        order_by="EmailThread.last_message_at",
    )


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

    # --- Win-likelihood score (see the module note on scoring) ---
    score = Column(Integer)             # 0-100, nullable = not scored
    score_reason = Column(Text)         # why you landed on that number
    scored_at = Column(DateTime)        # when the reading was taken

    # --- The two causes underneath that blended score ---
    #
    # Kept apart because they answer different questions and only one of them
    # is yours to fix. `score` says where the pursuit stands; these say why.
    # Both are 0-100 and nullable, and blank means "didn't judge it", not 0 --
    # the same rule `score` follows. Neither is required to set the other, and
    # nothing derives one from the other, because a call can genuinely go
    # (my_performance 80, employer_engagement 20) or the reverse, and
    # collapsing that into one axis destroys exactly the signal worth having.
    #
    # No `*_at` timestamps on these: they are attributes of the meeting, read
    # at the meeting's own date. `scored_at` exists only because score
    # calibration later needs to know when a judgment was formed, and these
    # two aren't fed into that analysis.
    my_performance = Column(Integer)        # 0-100: how well I did
    employer_engagement = Column(Integer)   # 0-100: how interested they were

    granola_note_id = Column(String(255), index=True)  # for linking / de-dup
    granola_link = Column(String(1024))

    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    application = relationship("JobApplication", back_populates="meetings")


# --------------------------------------------------------------------------- #
# Email Thread  (== Email/Task with multiple related Contacts): an exchange
# --------------------------------------------------------------------------- #
# Pure many-to-many join table, no extra columns and no distinguished
# "primary" person -- every row is just "this person is on this thread".
# CASCADE on both sides: deleting a thread clears its links; deleting a
# person clears theirs. Neither side cascades into deleting the *other*
# table's rows -- a thread survives losing one of several people on it, and
# a person survives being removed from a thread (see ARCHITECTURE.md for why
# a distinguished primary was dropped in favor of this).
email_thread_people = Table(
    "email_thread_people",
    Base.metadata,
    Column("email_thread_id", Integer, ForeignKey("email_threads.id", ondelete="CASCADE"), primary_key=True),
    Column("person_id", Integer, ForeignKey("people.id", ondelete="CASCADE"), primary_key=True),
)


class EmailThread(Base):
    """A separate object from Meeting, not folded into it -- the shape is
    different (subject/body/participants and a message span, vs. a single
    dated event) and, like a Person's own company, a thread can predate any
    Application: a cold recruiter email lands in your inbox before you've
    decided to pursue anything. It relates to People through a many-to-many
    join table (``email_thread_people``) rather than a single required FK,
    because a real thread often involves more than one person (an intro
    thread, a BCC'd hiring manager) and forcing a single "owner" misrepresents
    that. The Application link is an optional lookup that gets set once the
    thread is actually about a role you're pursuing.
    """

    __tablename__ = "email_threads"

    id = Column(Integer, primary_key=True)

    # Lookup to an application (nullable): unset for pre-application outreach.
    application_id = Column(
        Integer,
        ForeignKey("job_applications.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    subject = Column(String(512))
    body = Column(Text)                  # pasted thread content
    participants = Column(String(512))   # free text, e.g. "jane@co.com, me@gmail.com"

    started_at = Column(DateTime)        # first message in the thread
    # Most recent message -- drives ordering, including in the combined
    # Meetings+Emails activity timeline on the Application page, so a thread
    # with a fresh reply surfaces near the top rather than at its original date.
    last_message_at = Column(DateTime, index=True)

    notes = Column(Text)

    # --- Win-likelihood score (see the module note on scoring) ---
    score = Column(Integer)             # 0-100, nullable = not scored
    score_reason = Column(Text)         # why you landed on that number
    scored_at = Column(DateTime)        # when the reading was taken

    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    application = relationship("JobApplication", back_populates="email_threads")
    people = relationship(
        "Person",
        secondary=email_thread_people,
        back_populates="email_threads",
        order_by="Person.name",
    )


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
