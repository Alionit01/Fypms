"""Who may see contact details (audit SEC-04/REL-07).

Both predicates require an *explicit relationship* — being signed in is
not enough on its own, and neither is an invitation that has expired.
``main.py`` re-exports them because the test suite exercises the
relationship matrix directly.
"""

from sqlalchemy.orm import Session

from .. import models
from ..models import utc_now
from .invitations import _invitation_expired


def _may_view_contact(db: Session, requester_id: int, target_id: int) -> bool:
    """Contact details (email, whatsapp) require an explicit relationship.

    Allowed: the student themselves, a teammate (shared team), or a
    *live* team invitation in either direction. Being signed in is not
    enough on its own, and neither is an invitation that has expired —
    it is no longer a decision anyone can act on, so it must not keep
    leaking contact details forever.
    """
    if requester_id == target_id:
        return True

    requester_team_ids = {
        row.team_id
        for row in db.query(models.TeamMember).filter(
            models.TeamMember.student_id == requester_id
        )
    }
    if requester_team_ids and db.query(models.TeamMember).filter(
        models.TeamMember.student_id == target_id,
        models.TeamMember.team_id.in_(requester_team_ids),
    ).first():
        return True

    exchanged = db.query(models.TeamInvitation).filter(
        (
            (models.TeamInvitation.invited_by == requester_id)
            & (models.TeamInvitation.student_id == target_id)
        )
        | (
            (models.TeamInvitation.invited_by == target_id)
            & (models.TeamInvitation.student_id == requester_id)
        )
    ).all()
    now = utc_now()
    return any(not _invitation_expired(invitation, now) for invitation in exchanged)


def _may_view_team_contact(db: Session, team_id: int, requester_id: int) -> bool:
    """Team contact information requires an explicit relationship.

    Allowed: a current member of the team (the creator is always a
    member) or the student targeted by a *live* invitation to this
    team (so they can decide whether to accept). Being signed in is
    not enough on its own, and an expired invitation no longer counts.
    """
    if db.query(models.TeamMember).filter(
        models.TeamMember.team_id == team_id,
        models.TeamMember.student_id == requester_id,
    ).first():
        return True

    invitation = db.query(models.TeamInvitation).filter(
        models.TeamInvitation.team_id == team_id,
        models.TeamInvitation.student_id == requester_id,
    ).first()

    return invitation is not None and not _invitation_expired(invitation)
