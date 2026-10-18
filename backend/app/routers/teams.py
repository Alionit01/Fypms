"""Team lifecycle and membership endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models, schemas, database
from ..config import TEAM_DEPARTMENT_OPTIONS
from ..database import SessionLocal
from ..dependencies import get_current_student
from ..observability import log_event
from ..schemas import MAX_TEAM_MEMBERS, TeamCreate
from ..services.contacts import _may_view_team_contact
from ..services.directory import _contains, _search_pattern
from ..services.teams import _clamp_spots_available

router = APIRouter()


@router.post("/teams")
def create_team(
    team: TeamCreate,
    current_student=Depends(get_current_student)
):
    db = database.SessionLocal()

    try:
        if not current_student.profile_completed:
            raise HTTPException(
                status_code=403,
                detail="Complete your profile before creating a team"
            )

        # Serialize every membership decision for this student. Two
        # concurrent requests (e.g. creating a team while accepting an
        # invitation to a different team) previously raced: each locked a
        # *different* team row, so both passed the check below and both
        # inserted. The student row is the one both paths share. The
        # unique_student_team constraint (migration 0002) remains the
        # final authority if this lock is ever bypassed.
        db.query(models.Student).filter(
            models.Student.id == current_student.id
        ).with_for_update().first()

        # Student can only belong to one team
        existing_membership = db.query(models.TeamMember).filter(
            models.TeamMember.student_id == current_student.id
        ).first()

        if existing_membership:
            raise HTTPException(
                status_code=400,
                detail="You are already in a team"
            )

        # Validate department before creating anything
        if team.department_preference not in TEAM_DEPARTMENT_OPTIONS:
            raise HTTPException(
                status_code=400,
                detail="Invalid department preference"
            )

        # Validate spots (team total can never exceed MAX_TEAM_MEMBERS)
        if team.spots_available < 0:
            raise HTTPException(
                status_code=400,
                detail="Available spots cannot be negative"
            )
        if team.spots_available > MAX_TEAM_MEMBERS - 1:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"A team can have at most {MAX_TEAM_MEMBERS} members "
                    f"including the owner, so spots available cannot "
                    f"exceed {MAX_TEAM_MEMBERS - 1}."
                )
            )

        # Create team
        new_team = models.Team(
            name=team.name,
            project_title=team.project_title,
            description=team.description,
            department_preference=team.department_preference,
            spots_available=team.spots_available,
            skills_needed=team.skills_needed,
            roles_needed=team.roles_needed,
            contact=team.contact,
            created_by=current_student.id
        )

        db.add(new_team)
        db.flush()

        # Add creator as first member
        creator_member = models.TeamMember(
            team_id=new_team.id,
            student_id=current_student.id
        )

        db.add(creator_member)

        # Commit everything together
        db.commit()
        db.refresh(new_team)

        log_event("team_created", team_id=new_team.id,
                  owner_id=current_student.id)

        return {
            "message": "Team created successfully",
            "team_id": new_team.id
        }

    except HTTPException:
        db.rollback()
        raise

    except IntegrityError as exc:
        # The unique_student_team constraint is the authority for
        # "one team per student". If two requests raced past the
        # pre-check, the loser gets a constraint violation here rather
        # than an inconsistent membership. Report it as the business
        # rule it is, not as a server fault.
        db.rollback()
        if "unique_student_team" in str(exc.orig):
            raise HTTPException(
                status_code=400,
                detail="You are already in a team"
            )
        raise HTTPException(
            status_code=500,
            detail="Could not create team"
        )

    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail="Could not create team"
        )

    finally:
        db.close()

@router.put("/teams/{team_id}")
def update_team(
    team_id: int,
    team_data: schemas.TeamUpdate,
    current_student: models.Student = Depends(
        get_current_student
    )
):
    db: Session = SessionLocal()

    try:
        team = db.query(models.Team).filter(
            models.Team.id == team_id
        ).with_for_update().first()

        if not team:
            raise HTTPException(
                status_code=404,
                detail="Team not found"
            )

        if team.created_by != current_student.id:
            raise HTTPException(
                status_code=403,
                detail="Only the team owner can edit the team"
            )

        valid_departments = {
            "BSCS",
            "BSAI",
            "BSCB",
            "BSSE",
            "BESE",
            "Any",
        }

        if team_data.department_preference not in valid_departments:
            raise HTTPException(
                status_code=400,
                detail="Invalid department preference"
            )

        team.name = team_data.name
        team.project_title = team_data.project_title
        team.description = team_data.description
        team.department_preference = team_data.department_preference
        if team_data.spots_available > MAX_TEAM_MEMBERS - 1:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"A team can have at most {MAX_TEAM_MEMBERS} members "
                    f"including the owner, so spots available cannot "
                    f"exceed {MAX_TEAM_MEMBERS - 1}."
                )
            )
        team.spots_available = team_data.spots_available
        _clamp_spots_available(db, team)
        team.skills_needed = team_data.skills_needed
        team.roles_needed = team_data.roles_needed
        team.contact = team_data.contact

        db.commit()
        db.refresh(team)

        return {
            "message": "Team updated successfully",
            "team": {
                "id": team.id,
                "name": team.name,
                "project_title": team.project_title,
                "description": team.description,
                "department_preference": team.department_preference,
                "spots_available": team.spots_available,
                "skills_needed": team.skills_needed,
                "roles_needed": team.roles_needed,
                "contact": team.contact,
                "created_by": team.created_by,
            }
        }

    finally:
        db.close()

@router.delete("/teams/{team_id}")
def delete_team(
    team_id: int,
    current_student: models.Student = Depends(
        get_current_student
    )
):
    db: Session = SessionLocal()

    try:
        team = db.query(models.Team).filter(
            models.Team.id == team_id
        ).with_for_update().first()

        if not team:
            raise HTTPException(
                status_code=404,
                detail="Team not found"
            )

        # Only the team owner can delete the team
        if team.created_by != current_student.id:
            raise HTTPException(
                status_code=403,
                detail="Only the team owner can delete the team"
            )

        # Remove dependent records explicitly instead of relying on DB cascades.
        db.query(models.TeamInvitation).filter(
            models.TeamInvitation.team_id == team_id
        ).delete()
        db.query(models.TeamMember).filter(
            models.TeamMember.team_id == team_id
        ).delete()

        db.delete(team)
        db.commit()

        return {
            "message": "Team deleted successfully"
        }

    finally:
        db.close()


@router.delete("/teams/{team_id}/members/me")
def leave_team(
    team_id: int,
    current_student: models.Student = Depends(
        get_current_student
    )
):
    db: Session = SessionLocal()

    try:
        membership = db.query(models.TeamMember).filter(
            models.TeamMember.team_id == team_id,
            models.TeamMember.student_id == current_student.id
        ).first()

        if not membership:
            raise HTTPException(
                status_code=404,
                detail="You are not a member of this team"
            )

        team = db.query(models.Team).filter(
            models.Team.id == team_id
        ).with_for_update().first()

        if not team:
            raise HTTPException(
                status_code=404,
                detail="Team not found"
            )

        if team.created_by == current_student.id:
            # Owner leaving: hand leadership to the next member,
            # or dissolve the team if nobody else remains.
            db.delete(membership)
            db.flush()

            next_membership = db.query(models.TeamMember).filter(
                models.TeamMember.team_id == team_id
            ).order_by(models.TeamMember.id).first()

            if next_membership is None:
                db.query(models.TeamInvitation).filter(
                    models.TeamInvitation.team_id == team_id
                ).delete()
                db.delete(team)
                db.commit()
                return {
                    "message": "You left the team and it was removed"
                }

            db.query(models.TeamInvitation).filter(
                models.TeamInvitation.team_id == team_id
            ).delete()
            team.created_by = next_membership.student_id
            team.spots_available += 1
            _clamp_spots_available(db, team)
            db.commit()

            return {
                "message": "You left the team; leadership was transferred"
            }

        db.delete(membership)
        team.spots_available += 1
        _clamp_spots_available(db, team)

        db.commit()

        return {
            "message": "You left the team successfully"
        }

    finally:
        db.close()

@router.get("/teams")
def get_teams(
    skip: int = 0,
    limit: int = 50,
    search: str = "",
    program: str = "",
):
    """The team directory, filtered the same way the students one is.

    Anonymous access is kept as it was: teams are public, their
    members' details are not (that comes from GET /teams/{id}).
    """

    limit = min(max(limit, 1), 100)
    skip = max(skip, 0)

    db = database.SessionLocal()

    try:
        query = db.query(models.Team)

        program = (program or "").strip()
        if program and program != "All":
            # "Any Department" means the team takes anyone, so choosing
            # a program must still show it — exactly what the client
            # side filter used to do, and easy to lose here.
            query = query.filter(
                or_(
                    models.Team.department_preference == program,
                    models.Team.department_preference == "Any",
                )
            )

        term = (search or "").strip()
        if term:
            pattern = _search_pattern(term)
            query = query.filter(
                or_(
                    _contains(models.Team.name, pattern),
                    _contains(models.Team.project_title, pattern),
                    _contains(models.Team.description, pattern),
                    _contains(models.Team.skills_needed, pattern),
                    _contains(models.Team.roles_needed, pattern),
                )
            )

        teams = (
            query.order_by(models.Team.id).offset(skip).limit(limit).all()
        )

        return [
            {
                "id": team.id,
                "name": team.name,
                "project_title": team.project_title,
                "description": team.description,
                "department_preference": team.department_preference,
                "spots_available": team.spots_available,
                "skills_needed": team.skills_needed,
                "roles_needed": team.roles_needed,
                "created_by": team.created_by,
            }
            for team in teams
        ]
    finally:
        db.close()

@router.get("/teams/{team_id}")
def get_team(
    team_id: int,
    current_student=Depends(get_current_student),
):
    # Member names, programs, university IDs and profile pictures
    # are student information: anonymous requests are rejected by
    # get_current_student before any data is read (401). Any signed
    # in student may browse team details, mirroring the student
    # directory; contact details stay protected separately.

    db = database.SessionLocal()

    team = db.query(models.Team).filter(
        models.Team.id == team_id
    ).first()

    if not team:
        db.close()
        raise HTTPException(
            status_code=404,
            detail="Team not found"
        )

    # Find the team creator
    members = (
        db.query(models.Student)
        .join(
            models.TeamMember,
            models.TeamMember.student_id == models.Student.id
        )
        .filter(
            models.TeamMember.team_id == team.id
        )
        .all()
    )

    member_list = []

    for student in members:
        member_list.append({
            "id": student.id,
            "name": student.name,
            "program": student.program,
            "university_id": student.university_id,
            "profile_picture": student.profile_picture
        })

    result = {
        "id": team.id,
        "name": team.name,
        "project_title": team.project_title,
        "description": team.description,
        "department_preference": team.department_preference,
        "spots_available": team.spots_available,
        "skills_needed": team.skills_needed,
        "roles_needed": team.roles_needed,
        "created_by": team.created_by,
        "members": member_list
    }

    db.close()

    return result

@router.get("/teams/{team_id}/members")
def get_team_members(
    team_id: int,
    current_student=Depends(get_current_student),
):
    # Same protection as GET /teams/{team_id}: the roster is
    # student information and requires a valid sign-in.

    db = database.SessionLocal()

    team = db.query(models.Team).filter(
        models.Team.id == team_id
    ).first()

    if not team:
        db.close()
        raise HTTPException(
            status_code=404,
            detail="Team not found"
        )

    members = (
        db.query(models.Student)
        .join(
            models.TeamMember,
            models.TeamMember.student_id == models.Student.id
        )
        .filter(
            models.TeamMember.team_id == team_id
        )
        .all()
    )

    result = []

    for student in members:
        result.append({
            "id": student.id,
            "name": student.name,
            "university_id": student.university_id,
            "program": student.program,
            "profile_picture": student.profile_picture
        })

    db.close()

    return result

@router.get("/my-team")
def get_my_team(
    current_student=Depends(get_current_student)
):
    db = database.SessionLocal()

    membership = db.query(models.TeamMember).filter(
        models.TeamMember.student_id == current_student.id
    ).first()

    if not membership:
        db.close()
        return None

    team = db.query(models.Team).filter(
        models.Team.id == membership.team_id
    ).first()

    if not team:
        db.close()
        return None

    members = (
        db.query(models.Student)
        .join(
            models.TeamMember,
            models.TeamMember.student_id == models.Student.id
        )
        .filter(
            models.TeamMember.team_id == team.id
        )
        .all()
    )

    member_list = []

    for student in members:
        member_list.append({
            "id": student.id,
            "name": student.name,
            "university_id": student.university_id,
            "program": student.program,
            "profile_picture": student.profile_picture
        })

    result = {
        "id": team.id,
        "name": team.name,
        "project_title": team.project_title,
        "description": team.description,
        "department_preference": team.department_preference,
        "spots_available": team.spots_available,
        "skills_needed": team.skills_needed,
        "roles_needed": team.roles_needed,
        "contact": team.contact,
        "created_by": team.created_by,
        "members": member_list
    }

    db.close()

    return result


@router.get("/teams/{team_id}/contact")
def get_team_contact(
    team_id: int,
    current_student=Depends(get_current_student)
):
    db = database.SessionLocal()

    try:
        team = db.query(models.Team).filter(
            models.Team.id == team_id
        ).first()

        if not team:
            raise HTTPException(
                status_code=404,
                detail="Team not found"
            )

        if not _may_view_team_contact(db, team_id, current_student.id):
            raise HTTPException(
                status_code=403,
                detail=(
                    "Team contact information is only shared with team "
                    "members and invited students."
                ),
            )

        return {
            "contact": team.contact
        }
    finally:
        db.close()

@router.delete("/teams/{team_id}/members/{student_id}")
def remove_team_member(
    team_id: int,
    student_id: int,
    current_student: models.Student = Depends(get_current_student)
):
    db = database.SessionLocal()

    team = db.query(models.Team).filter(
        models.Team.id == team_id
    ).with_for_update().first()

    if not team:
        db.close()
        raise HTTPException(status_code=404, detail="Team not found")

    # Only the team owner can remove members
    if team.created_by != current_student.id:
        db.close()
        raise HTTPException(
            status_code=403,
            detail="Only the team owner can remove members"
        )

    # Owner cannot remove themselves
    if student_id == team.created_by:
        db.close()
        raise HTTPException(
            status_code=400,
            detail="Team owner cannot be removed"
        )

    membership = db.query(models.TeamMember).filter(
        models.TeamMember.team_id == team_id,
        models.TeamMember.student_id == student_id
    ).first()

    if not membership:
        db.close()
        raise HTTPException(
            status_code=404,
            detail="Student is not a member of this team"
        )

    db.delete(membership)

    # One spot becomes available again
    team.spots_available += 1
    _clamp_spots_available(db, team)

    db.commit()
    db.close()

    return {
        "message": "Student removed from team",
        "spots_available": team.spots_available
    }
