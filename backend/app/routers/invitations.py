"""Team-invitation endpoints: invite, manage, accept, decline."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from .. import models, database
from ..dependencies import get_current_student
from ..models import INVITATION_TTL, utc_now
from ..observability import log_event
from ..schemas import MAX_TEAM_MEMBERS
from ..services.directory import _directory_entry
from ..services.invitations import (
    _actionable_invitations,
    _invitation_expired,
    _member_count,
    _my_invitation_entry,
    _ordered_by_sent_at,
    _purge_expired_invitations,
    _sent_invitation_entry,
)
from ..services.teams import _clamp_spots_available

router = APIRouter()


@router.post("/teams/{team_id}/invitations/{student_id}")
def invite_team_member(
    team_id: int,
    student_id: int,
    current_student: models.Student = Depends(get_current_student)
):
    db = database.SessionLocal()
    try:
        team = db.query(models.Team).filter(
            models.Team.id == team_id
        ).with_for_update().first()
        if not team:
            raise HTTPException(status_code=404, detail="Team not found")
        if team.created_by != current_student.id:
            raise HTTPException(status_code=403, detail="Only the team owner can invite members")

        student = db.query(models.Student).filter(
            models.Student.id == student_id,
            models.Student.email_verified.is_(True)
        ).first()
        if not student:
            raise HTTPException(status_code=404, detail="That student could not be found")
        if db.query(models.TeamMember).filter(
            models.TeamMember.student_id == student_id
        ).first():
            raise HTTPException(
                status_code=400,
                detail=f"{student.name} is already in a team"
            )

        # Lapsed invitations for this team are cleared out under the
        # team lock, so re-inviting someone whose invitation expired is
        # a plain new invitation instead of a unique-constraint error.
        _purge_expired_invitations(db, team_id)
        existing = db.query(models.TeamInvitation).filter(
            models.TeamInvitation.team_id == team_id,
            models.TeamInvitation.student_id == student_id
        ).first()
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"{student.name} has already been invited to this team"
            )

        # Two capacity rules, in order of how surprising they are:
        # the spots the owner chose to advertise, then the hard team
        # size. Reserving a spot per pending invitation is what stops
        # six people being invited to a team with one place left, each
        # of whom would otherwise only discover the conflict after
        # accepting.
        pending = _actionable_invitations(db, team)
        if team.spots_available <= 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    "This team has no open spots. Edit the team to offer "
                    "more spots before inviting anyone."
                )
            )
        if len(pending) >= team.spots_available:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"All {team.spots_available} open "
                    f"{'spot is' if team.spots_available == 1 else 'spots are'} "
                    "already promised to invited students. Revoke an "
                    "invitation, or offer more spots, to invite someone else."
                )
            )
        if _member_count(db, team_id) + len(pending) >= MAX_TEAM_MEMBERS:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"All {MAX_TEAM_MEMBERS} spots are already taken or promised. "
                    "Revoke a pending invitation or remove a member first."
                )
            )

        db.add(models.TeamInvitation(
            team_id=team_id, student_id=student_id, invited_by=current_student.id
        ))
        message = (
            f"Invitation sent to {student.name}. They have to accept "
            "before joining."
        )

        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"{student.name} has already been invited to this team"
            )

        log_event(
            "team_invitation_sent",
            student_id=current_student.id,
            team_id=team_id,
            invitee_id=student_id,
        )
        invitation = db.query(models.TeamInvitation).filter(
            models.TeamInvitation.team_id == team_id,
            models.TeamInvitation.student_id == student_id
        ).first()
        return {
            "message": message,
            "invitation": _sent_invitation_entry(db, invitation, student),
        }
    finally:
        db.close()


@router.get("/teams/{team_id}/invitations")
def list_team_invitations(
    team_id: int,
    current_student: models.Student = Depends(get_current_student)
):
    """The owner's side of the conversation: who has been invited,
    when, and whether it is still open.

    Only the owner sees it — who a team has invited is team business.
    Invitations to students who joined another team are dropped: they
    can never be accepted, so managing them would be theatre.
    """
    db = database.SessionLocal()
    try:
        team = db.query(models.Team).filter(models.Team.id == team_id).first()
        if not team:
            raise HTTPException(status_code=404, detail="Team not found")
        if team.created_by != current_student.id:
            raise HTTPException(
                status_code=403,
                detail="Only the team owner can manage invitations"
            )

        # Expiry is already filtered out of the list below; expired rows
        # are physically removed when the owner next sends or re-sends
        # an invitation (see invite_team_member), where a write is part
        # of the transaction anyway.
        invitations = db.query(models.TeamInvitation).filter(
            models.TeamInvitation.team_id == team_id
        ).all()
        # Same rule as the capacity count: an invitation to someone who
        # is already on a team can never be accepted, so it is not
        # offered back as something to manage.
        in_any_team_ids = {
            student_id for (student_id,) in db.query(
                models.TeamMember.student_id
            ).filter(
                models.TeamMember.student_id.in_(
                    [invitation.student_id for invitation in invitations]
                )
            ).all()
        } if invitations else set()

        entries = _ordered_by_sent_at([
            _sent_invitation_entry(db, invitation)
            for invitation in invitations
            if invitation.student_id not in in_any_team_ids
        ])
        member_count = _member_count(db, team_id)
        open_count = sum(1 for entry in entries if entry["status"] == "pending")

        # How many more invitations this team may actually send: bounded
        # by the hard team size AND by the spots the owner chose to
        # advertise (a team that closed its spots can promise nothing
        # new, however many places the arithmetic would allow).
        return {
            "invitations": entries,
            "member_count": member_count,
            "spots_available": team.spots_available,
            "open_invitations": open_count,
            "invitable_spots": max(0, min(
                team.spots_available,
                MAX_TEAM_MEMBERS - member_count - open_count,
            )),
        }
    finally:
        db.close()


@router.delete("/teams/{team_id}/invitations/{invitation_id}")
def revoke_team_invitation(
    team_id: int,
    invitation_id: int,
    current_student: models.Student = Depends(get_current_student)
):
    """Withdraw an invitation the owner no longer stands behind."""
    db = database.SessionLocal()
    try:
        team = db.query(models.Team).filter(
            models.Team.id == team_id
        ).with_for_update().first()
        if not team:
            raise HTTPException(status_code=404, detail="Team not found")
        if team.created_by != current_student.id:
            raise HTTPException(
                status_code=403,
                detail="Only the team owner can manage invitations"
            )

        invitation = db.query(models.TeamInvitation).filter(
            models.TeamInvitation.id == invitation_id,
            models.TeamInvitation.team_id == team_id
        ).with_for_update().first()
        if not invitation:
            raise HTTPException(status_code=404, detail="Invitation not found")

        invitee_id = invitation.student_id
        db.delete(invitation)
        db.commit()

        log_event(
            "team_invitation_revoked",
            student_id=current_student.id,
            team_id=team_id,
            invitee_id=invitee_id,
        )
        return {"message": "Invitation revoked"}
    finally:
        db.close()


@router.post("/invitations/{invitation_id}/resend")
def resend_team_invitation(
    invitation_id: int,
    current_student: models.Student = Depends(get_current_student)
):
    """Give a lapsed (or about to lapse) invitation a fresh deadline."""
    db = database.SessionLocal()
    try:
        invitation_team = db.query(models.TeamInvitation.team_id).filter(
            models.TeamInvitation.id == invitation_id
        ).first()
        if not invitation_team:
            raise HTTPException(status_code=404, detail="Invitation not found")

        team = db.query(models.Team).filter(
            models.Team.id == invitation_team.team_id
        ).with_for_update().first()
        if not team:
            raise HTTPException(status_code=404, detail="Team not found")

        invitation = db.query(models.TeamInvitation).filter(
            models.TeamInvitation.id == invitation_id
        ).with_for_update().first()
        if not invitation or invitation.team_id != team.id:
            raise HTTPException(status_code=404, detail="Invitation not found")
        if team.created_by != current_student.id:
            raise HTTPException(
                status_code=403,
                detail="Only the team owner can manage invitations"
            )
        if invitation.invited_by != team.created_by:
            raise HTTPException(
                status_code=409,
                detail="This invitation is no longer valid. Send a new one instead."
            )

        student = db.query(models.Student).filter(
            models.Student.id == invitation.student_id
        ).first()
        if not student:
            raise HTTPException(status_code=404, detail="That student could not be found")
        if db.query(models.TeamMember).filter(
            models.TeamMember.student_id == invitation.student_id
        ).first():
            raise HTTPException(
                status_code=409,
                detail=f"{student.name} has already joined another team"
            )
        if _member_count(db, team.id) >= MAX_TEAM_MEMBERS:
            raise HTTPException(
                status_code=409,
                detail="This team is full, so the invitation cannot be re-sent"
            )
        # The spot must still be free for this student: counting the
        # other pending invitations only, not the one being renewed.
        promised_elsewhere = [
            pending for pending in _actionable_invitations(db, team)
            if pending.id != invitation.id
        ]
        if len(promised_elsewhere) >= team.spots_available:
            raise HTTPException(
                status_code=409,
                detail=(
                    "That spot has already been promised to another student. "
                    "Revoke that invitation first."
                )
            )

        invitation.created_at = utc_now()
        invitation.expires_at = utc_now() + INVITATION_TTL
        db.commit()

        log_event(
            "team_invitation_resent",
            student_id=current_student.id,
            team_id=team.id,
            invitee_id=invitation.student_id,
        )
        return {
            "message": f"Invitation re-sent to {student.name}",
            "invitation": _sent_invitation_entry(db, invitation, student),
        }
    finally:
        db.close()


@router.get("/teams/{team_id}/invitations/candidates")
def invitation_candidates(
    team_id: int,
    query: str = "",
    limit: int = 20,
    current_student: models.Student = Depends(get_current_student)
):
    """Students an owner could invite, searched rather than paginated.

    The old flow offered a dropdown over the first 50 rows of the
    directory, which on any real cohort is a random 50 people — most of
    them already in a team, none of them findable. This searches by
    name, program or university id and labels every result, so the
    owner sees *why* someone can or cannot be invited before clicking.
    """
    limit = min(max(limit, 1), 50)

    db = database.SessionLocal()
    try:
        team = db.query(models.Team).filter(models.Team.id == team_id).first()
        if not team:
            raise HTTPException(status_code=404, detail="Team not found")
        if team.created_by != current_student.id:
            raise HTTPException(
                status_code=403,
                detail="Only the team owner can invite members"
            )

        search = query.strip()
        students = db.query(models.Student).filter(
            models.Student.email_verified.is_(True),
            models.Student.id != current_student.id,
        )
        if search:
            pattern = f"%{search.lower()}%"
            students = students.filter(
                func.lower(models.Student.name).like(pattern)
                | func.lower(models.Student.program).like(pattern)
                | func.lower(models.Student.university_id).like(pattern)
            )

        # Over-fetch, then rank: with a full cohort a plain LIMIT would
        # page past every invitable student into the "already in a team"
        # ones. Bounded so a broad search cannot pull the directory.
        matches = students.order_by(models.Student.name).limit(limit * 5).all()
        if not matches:
            return {"results": [], "query": search}

        team_invitations = db.query(models.TeamInvitation).filter(
            models.TeamInvitation.team_id == team_id
        ).all()
        # A lapsed invitation holds nothing: its student is invitable
        # again, not "already invited".
        now = utc_now()
        invited_ids = {
            invitation.student_id for invitation in team_invitations
            if not _invitation_expired(invitation, now)
        }
        in_any_team_ids = {
            student_id for (student_id,) in db.query(
                models.TeamMember.student_id
            ).filter(
                models.TeamMember.student_id.in_([student.id for student in matches])
            ).all()
        }

        entries = []
        for student in matches:
            if student.id in in_any_team_ids:
                status = "in-team"
            elif student.id in invited_ids:
                status = "invited"
            else:
                status = "invitable"
            entries.append({
                **_directory_entry(student, include_picture=False),
                "status": status,
            })

        # Invitable first, then already-invited, then unavailable —
        # the top of the list is always the action that can succeed.
        rank = {"invitable": 0, "invited": 1, "in-team": 2}
        entries.sort(key=lambda entry: (rank[entry["status"]], entry["name"]))
        return {"results": entries[:limit], "query": search}
    finally:
        db.close()


@router.get("/my-invitations")
def get_my_invitations(current_student: models.Student = Depends(get_current_student)):
    db = database.SessionLocal()
    try:
        # A student already on a team can never accept another
        # invitation (accept_invitation returns 409), so hiding the
        # pending list here keeps the UI from offering a dead-end
        # "Accept" button. Stale invitations left over from before they
        # joined a team are simply not surfaced.
        if db.query(models.TeamMember).filter(
            models.TeamMember.student_id == current_student.id
        ).first():
            return []

        rows = db.query(models.TeamInvitation, models.Team).join(
            models.Team, models.Team.id == models.TeamInvitation.team_id
        ).filter(models.TeamInvitation.student_id == current_student.id).all()

        entries = []
        now = utc_now()
        for invitation, team in rows:
            # An invitation from a previous owner no longer speaks for
            # the team, and an expired one is no longer a choice — both
            # are dropped rather than offered as buttons that can only
            # fail.
            if invitation.invited_by != team.created_by:
                continue
            if _invitation_expired(invitation, now):
                continue
            entries.append(_my_invitation_entry(db, invitation, team))

        return _ordered_by_sent_at(entries)
    finally:
        db.close()


@router.post("/invitations/{invitation_id}/accept")
def accept_invitation(
    invitation_id: int,
    current_student: models.Student = Depends(get_current_student)
):
    db = database.SessionLocal()
    try:
        # Lock the student row FIRST: it is the one resource shared by
        # every concurrent "join a team" attempt from this student.
        # Locking only the team (as before) left a cross-team race open,
        # because two accepts for two different teams locked two
        # different rows and both passed the membership check below.
        # The unique_student_team constraint (migration 0002) is the
        # final authority if this lock is bypassed.
        db.query(models.Student).filter(
            models.Student.id == current_student.id
        ).with_for_update().first()

        # Lock the team before the invitation so member-count and spot updates
        # are serialized with other team mutations.
        invitation_team = db.query(models.TeamInvitation.team_id).filter(
            models.TeamInvitation.id == invitation_id,
            models.TeamInvitation.student_id == current_student.id
        ).first()
        if not invitation_team:
            raise HTTPException(status_code=404, detail="Invitation not found")
        team = db.query(models.Team).filter(
            models.Team.id == invitation_team.team_id
        ).with_for_update().first()
        if not team:
            raise HTTPException(status_code=404, detail="Team not found")
        invitation = db.query(models.TeamInvitation).filter(
            models.TeamInvitation.id == invitation_id,
            models.TeamInvitation.student_id == current_student.id
        ).with_for_update().first()
        if not invitation or invitation.team_id != team.id:
            raise HTTPException(status_code=404, detail="Invitation not found")
        if invitation.invited_by != team.created_by:
            raise HTTPException(status_code=409, detail="Invitation is no longer valid")
        if _invitation_expired(invitation):
            raise HTTPException(
                status_code=409,
                detail="This invitation has expired. Ask the team owner to send a new one."
            )
        if db.query(models.TeamMember).filter(
            models.TeamMember.student_id == current_student.id
        ).first():
            raise HTTPException(status_code=409, detail="You are already in a team")
        if team.spots_available <= 0 or db.query(models.TeamMember).filter(
            models.TeamMember.team_id == team.id
        ).count() >= MAX_TEAM_MEMBERS:
            raise HTTPException(status_code=409, detail="This team has no available spots")

        db.add(models.TeamMember(team_id=team.id, student_id=current_student.id))
        db.delete(invitation)
        team.spots_available -= 1
        _clamp_spots_available(db, team)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            # unique_student_team fired: this student reached another
            # team between our check and our insert. Surface the real
            # business rule instead of a generic failure.
            if "unique_student_team" in str(exc.orig):
                log_event(
                    "team_join_rejected",
                    reason="already_in_a_team",
                    student_id=current_student.id,
                    team_id=invitation.team_id,
                )
                raise HTTPException(
                    status_code=409, detail="You are already in a team"
                )
            raise HTTPException(status_code=409, detail="Could not join the team")

        log_event("team_joined", student_id=current_student.id,
                  team_id=invitation.team_id, invitation_id=invitation_id)
        return {"message": f"You joined {team.name}"}
    finally:
        db.close()


@router.delete("/invitations/{invitation_id}")
def decline_invitation(
    invitation_id: int,
    current_student: models.Student = Depends(get_current_student)
):
    db = database.SessionLocal()
    try:
        invitation = db.query(models.TeamInvitation).filter(
            models.TeamInvitation.id == invitation_id,
            models.TeamInvitation.student_id == current_student.id
        ).first()
        if not invitation:
            raise HTTPException(status_code=404, detail="Invitation not found")
        db.delete(invitation)
        db.commit()
        return {"message": "Invitation declined"}
    finally:
        db.close()
