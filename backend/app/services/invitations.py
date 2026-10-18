"""Team-invitation helpers shared by the invitation endpoints.

An invitation is a proposal, not a reservation: the invitee decides,
and the team keeps the spot until they say yes. That stays fair only
if both sides can see and undo what the other side has done, so:

  * the invitee gets a real inbox entry (who invited them, which
    team, how big it is, when it expires) instead of a bare team
    name with two buttons,
  * the owner gets a "sent" list with revoke and resend, instead of
    sending into a void and discovering the result only when the
    student shows up as a member,
  * an invitation expires, so neither side is left holding a
    months-old maybe that can still change the team at any moment.
"""

from datetime import timezone

from .. import models
from ..models import utc_now


def _as_utc(value):
    """Normalize a stored timestamp for comparison with utc_now().

    SQLite returns naive datetimes even for timezone-aware columns, so
    `value > utc_now()` raises TypeError without this. Everything here
    is stored in UTC (models.utc_now), so attaching it back is correct.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _iso(value):
    """An ISO-8601 UTC string for the API, or None when unknown."""
    normalized = _as_utc(value)
    return normalized.isoformat() if normalized else None


def _invitation_expired(invitation, now=None) -> bool:
    """True once the invitation's deadline has passed.

    A NULL expiry (a row written before migration 0003 backfilled it)
    counts as "no deadline" rather than silently dropping a live
    invitation.
    """
    expires_at = _as_utc(invitation.expires_at)
    if expires_at is None:
        return False
    return (now or utc_now()) >= expires_at


def _invitation_days_remaining(invitation, now=None) -> int:
    """Whole days left, rounded up — "1 day" for the last 24 hours,
    0 once expired. None when the invitation has no deadline."""
    expires_at = _as_utc(invitation.expires_at)
    if expires_at is None:
        return None
    seconds = (expires_at - (now or utc_now())).total_seconds()
    if seconds <= 0:
        return 0
    return int(seconds // 86400) + (1 if seconds % 86400 else 0)


def _actionable_invitations(db, team):
    """The team's invitations that could still become memberships.

    Three things silently void an invitation, and each one has to stop
    holding a spot on the team:

      * the invitee joined another team (they can no longer accept),
      * the deadline passed,
      * the team changed owner (the invitation came from someone who
        no longer speaks for the team).

    Rows are kept rather than deleted — the audit trail and a later
    "leave the team" both want them — but they never count against the
    team's capacity from here on.
    """
    invitations = db.query(models.TeamInvitation).filter(
        models.TeamInvitation.team_id == team.id
    ).all()
    if not invitations:
        return []

    member_ids = {
        student_id for (student_id,) in db.query(
            models.TeamMember.student_id
        ).filter(
            models.TeamMember.student_id.in_(
                [invitation.student_id for invitation in invitations]
            )
        ).all()
    }

    now = utc_now()
    return [
        invitation
        for invitation in invitations
        if invitation.invited_by == team.created_by
        and invitation.student_id not in member_ids
        and not _invitation_expired(invitation, now)
    ]


def _member_count(db, team_id) -> int:
    return db.query(models.TeamMember).filter(
        models.TeamMember.team_id == team_id
    ).count()


def _my_invitation_entry(db, invitation, team) -> dict:
    """An inbox entry: everything the invitee needs to decide."""
    inviter = db.query(models.Student).filter(
        models.Student.id == invitation.invited_by
    ).first()
    member_count = _member_count(db, team.id)

    return {
        "id": invitation.id,
        "team_id": team.id,
        "team_name": team.name,
        "project_title": team.project_title,
        "department_preference": team.department_preference,
        "member_count": member_count,
        "spots_available": team.spots_available,
        "inviter": (
            {
                "id": inviter.id,
                "name": inviter.name,
                "program": inviter.program,
            }
            if inviter
            else None
        ),
        "created_at": _iso(invitation.created_at),
        "expires_at": _iso(invitation.expires_at),
        "days_remaining": _invitation_days_remaining(invitation),
    }


def _sent_invitation_entry(db, invitation, student=None) -> dict:
    """A sent-invitation row for the owner's management list."""
    if student is None:
        student = db.query(models.Student).filter(
            models.Student.id == invitation.student_id
        ).first()
    expired = _invitation_expired(invitation)

    return {
        "id": invitation.id,
        "team_id": invitation.team_id,
        "student_id": invitation.student_id,
        "student": (
            {
                "id": student.id,
                "name": student.name,
                "program": student.program,
                "university_id": student.university_id,
                "profile_picture": student.profile_picture,
            }
            if student
            else None
        ),
        "status": "expired" if expired else "pending",
        "created_at": _iso(invitation.created_at),
        "expires_at": _iso(invitation.expires_at),
        "days_remaining": _invitation_days_remaining(invitation),
    }


def _ordered_by_sent_at(entries):
    """Newest invitation first — the one just sent is the one in view."""
    return sorted(
        entries,
        key=lambda entry: entry["created_at"] or "",
        reverse=True,
    )


def _purge_expired_invitations(db, team_id=None) -> None:
    """Delete invitations past their deadline.

    Expiry is enforced on read everywhere it matters, so this is
    housekeeping rather than a correctness requirement — but without it
    the table keeps rows nobody can act on, and the unique constraint
    would block a legitimate re-invite of the same student.
    """
    query = db.query(models.TeamInvitation).filter(
        models.TeamInvitation.expires_at.isnot(None),
        models.TeamInvitation.expires_at <= utc_now(),
    )
    if team_id is not None:
        query = query.filter(models.TeamInvitation.team_id == team_id)
    query.delete(synchronize_session=False)
