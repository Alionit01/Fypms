"""Student directory and profile endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_

from .. import models, database
from ..config import FYP_STATUSES, PROGRAMS
from ..dependencies import get_current_student
from ..schemas import StudentUpdate
from ..services.contacts import _may_view_contact
from ..services.directory import (
    _contains,
    _directory_entry,
    _search_pattern,
)

router = APIRouter()


@router.get("/students")
def get_students(
    skip: int = 0,
    limit: int = 50,
    search: str = "",
    program: str = "",
    status: str = "",
    current_student=Depends(get_current_student),
):
    """The student directory, filtered server-side.

    Filtering used to happen in the browser over whichever 50 rows had
    been fetched, so a classmate on page two was reported as "no
    students found". The filters now narrow the query itself and
    pagination walks the *filtered* set in a stable order.
    """

    limit = min(max(limit, 1), 100)
    skip = max(skip, 0)

    db = database.SessionLocal()

    try:
        query = db.query(models.Student)

        program = (program or "").strip()
        if program and program != "All":
            query = query.filter(models.Student.program == program)

        status = (status or "").strip()
        if status and status != "All":
            query = query.filter(models.Student.fyp_status == status)

        term = (search or "").strip()
        if term:
            pattern = _search_pattern(term)
            query = query.filter(
                or_(
                    _contains(models.Student.name, pattern),
                    _contains(models.Student.program, pattern),
                    _contains(models.Student.university_id, pattern),
                    _contains(models.Student.skills, pattern),
                    _contains(models.Student.interests, pattern),
                )
            )

        students = (
            query.order_by(models.Student.id).offset(skip).limit(limit).all()
        )
        return [
            _directory_entry(student, include_picture=False)
            for student in students
        ]
    finally:
        db.close()

@router.get("/students/{student_id}")
def get_student(
    student_id: int,
    current_student=Depends(get_current_student),
):

    db = database.SessionLocal()

    student = db.query(models.Student).filter(
        models.Student.id == student_id
    ).first()

    db.close()

    if not student:
        raise HTTPException(
            status_code=404,
            detail="Student not found"
        )

    return _directory_entry(student)


@router.get("/students/{student_id}/contact")
def get_student_contact(
    student_id: int,
    current_student=Depends(get_current_student)
):

    db = database.SessionLocal()

    try:
        student = db.query(models.Student).filter(
            models.Student.id == student_id
        ).first()

        if not student:
            raise HTTPException(
                status_code=404,
                detail="Student not found"
            )

        if not _may_view_contact(db, current_student.id, student_id):
            raise HTTPException(
                status_code=403,
                detail=(
                    "Contact details are only shared with teammates and "
                    "students who have exchanged team invitations."
                ),
            )

        return {
            "email": student.email,
            "whatsapp": student.whatsapp
        }
    finally:
        db.close()

@router.put("/my-profile")
def update_my_profile(
    student_data: StudentUpdate,
    current_student=Depends(get_current_student)
):
    db = database.SessionLocal()

    try:
        student = db.query(models.Student).filter(
            models.Student.id == current_student.id
        ).first()

        if not student:
            raise HTTPException(
                status_code=404,
                detail="Student not found"
            )

        if student_data.program not in PROGRAMS:
            raise HTTPException(
                status_code=400,
                detail="Invalid program"
            )

        if (
                student_data.fyp_status
                and student_data.fyp_status not in FYP_STATUSES
        ):
            raise HTTPException(
                status_code=400,
                detail="Invalid FYP status"
            )

        student.name = student_data.name
        student.program = student_data.program
        student.profile_picture = student_data.profile_picture
        student.bio = student_data.bio
        student.github = student_data.github
        student.linkedin = student_data.linkedin
        student.whatsapp = student_data.whatsapp
        student.skills = student_data.skills
        student.interests = student_data.interests
        student.fyp_status = student_data.fyp_status

        # One-way: flip to TRUE once the required fields are present.
        # Never downgrade so grandfathered users are not locked out.
        if (
            not student.profile_completed
            and student.skills
            and student.skills.strip()
            and student.fyp_status
        ):
            student.profile_completed = True

        db.commit()

        profile = {
            "id": student.id,
            "name": student.name,
            "university_id": student.university_id,
            "email": student.email,
            "program": student.program,
            "profile_picture": student.profile_picture,
            "bio": student.bio,
            "github": student.github,
            "linkedin": student.linkedin,
            "whatsapp": student.whatsapp,
            "skills": student.skills,
            "interests": student.interests,
            "fyp_status": student.fyp_status,
            "profile_completed": student.profile_completed
        }

        return {
            "message": "Profile updated successfully",
            "profile": profile
        }
    finally:
        db.close()

@router.get("/my-profile")
def get_my_profile(
    current_student=Depends(get_current_student)
):
    return {
        "id": current_student.id,
        "name": current_student.name,
        "university_id": current_student.university_id,
        "email": current_student.email,
        "program": current_student.program,
        "profile_picture": current_student.profile_picture,
        "bio": current_student.bio,
        "github": current_student.github,
        "linkedin": current_student.linkedin,
        "whatsapp": current_student.whatsapp,
        "skills": current_student.skills,
        "interests": current_student.interests,
        "fyp_status": current_student.fyp_status,
        "profile_completed": current_student.profile_completed
    }
