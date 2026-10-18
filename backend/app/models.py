from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from .database import Base


# How long a team invitation stays actionable. Without a deadline a
# sent invitation used to live forever, so an owner's "pending" list
# slowly filled with people who had long since lost interest, and the
# student it was addressed to kept an Accept button that could join a
# team they no longer wanted. An expiry turns a silent maybe into a
# clear "ask again" (see INVITATION_TTL_DAYS in the frontend mirror).
INVITATION_TTL_DAYS = 14
INVITATION_TTL = timedelta(days=INVITATION_TTL_DAYS)


def utc_now() -> datetime:
    """Timezone-aware UTC now. Stored values come back naive from
    SQLite, so readers normalize with `.replace(tzinfo=timezone.utc)`."""
    return datetime.now(timezone.utc)


# Application-controlled roles. The role column is written only by this
# application: request payloads never carry a role (StudentCreate has no
# role field and Pydantic ignores unknown keys), and the sole promotion
# path is the one-time bootstrap script scripts/make_admin.py.
ADMIN_ROLE = "admin"
STUDENT_ROLE = "student"
VALID_ROLES = {STUDENT_ROLE, ADMIN_ROLE}


class Student(Base):
    __tablename__ = "students"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    university_id = Column(String, unique=True, nullable=False)
    email = Column(String, unique=True, nullable=False)
    password = Column(String, nullable=False)
    program = Column(String, nullable=False)
    profile_picture = Column(String, nullable=True)
    bio = Column(Text, nullable=True)
    github = Column(String, nullable=True)
    linkedin = Column(String, nullable=True)
    whatsapp = Column(String, nullable=True)
    skills = Column(Text, nullable=True)
    interests = Column(Text, nullable=True)
    fyp_status = Column(String, nullable=True)
    email_verified = Column(
        Boolean, nullable=False, default=False
    )
    verification_token = Column(String, nullable=True)
    # Authorization role; existing and new accounts default to
    # 'student'. Never accept this from a request payload; the only
    # 'admin' writer is scripts/make_admin.py (see VALID_ROLES).
    role = Column(
        String,
        nullable=False,
        default=STUDENT_ROLE,
        # SQLAlchemy quotes plain-string server_defaults itself, so this
        # renders as DEFAULT 'student' in DDL.
        server_default=STUDENT_ROLE,
    )
    # New accounts start incomplete; the startup migration backfills
    # existing rows as TRUE (server_default), grandfathering them.
    profile_completed = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="true",
    )
    # Temporary supervised-pilot marker (see pilot_onboarding_enabled
    # in app/main.py). Pilot accounts are created UNVERIFIED:
    # email_verified is never set by the pilot path, so this flag
    # only records the signup route. The login policy in main.py
    # gates pilot accounts on the flag being explicitly enabled.
    # Request payloads never carry this field (StudentCreate has no
    # such key and Pydantic ignores unknown keys).
    pilot_account = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )

class PendingRegistration(Base):
    __tablename__ = "pending_registrations"

    id = Column(Integer, primary_key=True)
    token_hash = Column(String(64), unique=True, nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    student_data = Column(JSON, nullable=False)


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id = Column(Integer, primary_key=True)
    token_hash = Column(String(64), unique=True, nullable=False, index=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)


class Team(Base):
    __tablename__ = "teams"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    project_title = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    department_preference = Column(String, nullable=False)
    spots_available = Column(Integer, nullable=False)
    skills_needed = Column(Text, nullable=True)
    roles_needed = Column(Text, nullable=True)
    contact = Column(String, nullable=True)
    created_by = Column(
        Integer,
        ForeignKey("students.id"),
        nullable=False
    )

class TeamInvitation(Base):
    __tablename__ = "team_invitations"

    id = Column(Integer, primary_key=True)
    team_id = Column(
        Integer, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    student_id = Column(
        Integer, ForeignKey("students.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    invited_by = Column(
        Integer, ForeignKey("students.id"), nullable=False
    )
    # When the invitation was sent, and when it stops being actionable.
    # Row existence alone is no longer "pending": an expired invitation
    # is hidden from the invitee, cannot be accepted, and no longer
    # holds a spot on the team (the owner can resend it instead).
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    expires_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: utc_now() + INVITATION_TTL,
        # Indexed because _purge_expired_invitations filters on
        # expires_at <= now() — without this it is a full table scan.
        index=True,
    )

    __table_args__ = (
        UniqueConstraint("team_id", "student_id", name="unique_team_invitee"),
    )


class TeamMember(Base):
    __tablename__ = "team_members"

    id = Column(Integer, primary_key=True, index=True)

    team_id = Column(
        Integer,
        ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    student_id = Column(
        Integer,
        ForeignKey("students.id", ondelete="CASCADE"),
        nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "team_id",
            "student_id",
            name="unique_team_student"
        ),
        UniqueConstraint(
            "student_id",
            name="unique_student_team"
        ),
    )