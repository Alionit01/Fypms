"""Registration, verification, login and password-reset endpoints."""

import hashlib
import os
import secrets
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from .. import models, schemas, database
from ..auth import hash_password, verify_password, create_access_token
from ..config import (
    FYP_STATUSES,
    PROGRAMS,
    VERIFICATION_TTL,
    pilot_onboarding_enabled,
)
from ..dependencies import get_current_student
from ..observability import log_event
from ..schemas import StudentCreate, StudentLogin, VerificationRequest
from ..services.rate_limit import (
    _check_login_rate_limit,
    _client_ip,
    _record_login_failure,
)

router = APIRouter()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _send_verification_email(email: str, token: str) -> None:
    """Send the verification mail, resolved through ``app.main``.

    The test suite patches ``main.send_verification_email`` to capture
    or fail delivery; going through the ``main`` module attribute keeps
    those patches effective now that the endpoint lives in this router.
    """
    from .. import main as _main
    _main.send_verification_email(email, token)


def _send_password_reset_email(email: str, token: str) -> None:
    """Send the reset mail, resolved through ``app.main``.

    See ``_send_verification_email``: tests patch
    ``main.send_password_reset_email``.
    """
    from .. import main as _main
    _main.send_password_reset_email(email, token)


@router.post("/students", status_code=202)
def create_student(student: StudentCreate):
    university_domain = os.getenv("UNIVERSITY_EMAIL_DOMAIN")
    if not university_domain:
        raise HTTPException(status_code=500, detail="University email domain is not configured")

    email = student.email.lower()
    if not email.endswith("@" + university_domain.lower()):
        raise HTTPException(status_code=403, detail="University email required")

    # The university ID must appear in the email local part: either the
    # whole local part (67267@iqra.edu.pk) or after a dot
    # (ali.67267@iqra.edu.pk). Comparison is case-insensitive.
    local_part = email.rsplit("@", 1)[0]
    university_id = student.university_id.strip().lower()
    if not university_id or (
        local_part != university_id
        and not local_part.endswith("." + university_id)
    ):
        raise HTTPException(
            status_code=400, detail="University ID must match your email address"
        )
    if student.program not in PROGRAMS:
        raise HTTPException(status_code=400, detail="Invalid program")
    if student.fyp_status and student.fyp_status not in FYP_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid FYP status")

    db = database.SessionLocal()
    token = secrets.token_urlsafe(32)
    try:
        db.query(models.PendingRegistration).filter(
            models.PendingRegistration.expires_at <= datetime.now(timezone.utc)
        ).delete()
        if db.query(models.Student).filter(
            models.Student.email_verified.is_(True),
            (models.Student.email == email) |
            (func.lower(models.Student.university_id) == student.university_id.strip().lower())
        ).first():
            raise HTTPException(status_code=400, detail="Email or university ID already registered")

        if pilot_onboarding_enabled():
            # Supervised pilot: create the account immediately so
            # students can register and sign in without email
            # delivery. The account stays UNVERIFIED
            # (email_verified=False) — the pilot never verifies an
            # email address — and the login policy below admits it
            # only while the pilot flag is explicitly enabled.
            if not student.password:
                raise HTTPException(
                    status_code=400,
                    detail="A password is required to create your account"
                )
            # Stricter than the normal flow: during the pilot any
            # existing row (verified or not) blocks re-registration,
            # so an account cannot be duplicated or overwritten by
            # re-registering with the same email or university ID.
            if db.query(models.Student).filter(
                (models.Student.email == email) |
                (func.lower(models.Student.university_id) == student.university_id.strip().lower())
            ).first():
                raise HTTPException(status_code=400, detail="Email or university ID already registered")

            new_student = models.Student(
                name=student.name,
                email=email,
                university_id=student.university_id.strip().upper(),
                program=student.program,
                fyp_status=student.fyp_status,
                password=hash_password(student.password),
                email_verified=False,
                pilot_account=True,
            )
            db.add(new_student)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                raise HTTPException(
                    status_code=409,
                    detail="Email or university ID already registered"
                )
            db.refresh(new_student)

            # Sign the student in immediately (same token shape as
            # /login) so the QR-code flow is one step.
            return {
                "message": "Pilot account created. You are signed in.",
                "access_token": create_access_token(new_student.id),
                "token_type": "bearer",
                "student_id": new_student.id,
            }

        # The pilot-only password field must never be
        # persisted: pending registrations hold no password.
        data = student.model_dump(exclude={"password"})
        data["email"] = email
        data["university_id"] = student.university_id.strip().upper()
        pending = models.PendingRegistration(
            token_hash=_token_hash(token),
            expires_at=datetime.now(timezone.utc) + VERIFICATION_TTL,
            student_data=data,
        )
        db.add(pending)
        db.commit()

        try:
            _send_verification_email(email, token)
        except Exception:
            # SMTP libraries raise several types of errors. Do not expose
            # the message or token in a response or log.
            db.delete(pending)
            db.commit()
            raise HTTPException(status_code=503, detail="Could not send verification email") from None

        return {"message": "Check your university email for a verification link."}
    finally:
        db.close()


@router.post("/verify-email")
def verify_email(request: VerificationRequest):
    token = request.token
    db = database.SessionLocal()
    try:
        pending = db.query(models.PendingRegistration).filter(
            models.PendingRegistration.token_hash == _token_hash(token)
        ).with_for_update().first()
        if not pending:
            raise HTTPException(status_code=400, detail="Invalid or expired verification token")

        expires_at = pending.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) >= expires_at:
            db.delete(pending)
            db.commit()
            raise HTTPException(status_code=400, detail="Invalid or expired verification token")

        data = dict(pending.student_data)
        data["password"] = hash_password(request.password)
        existing = db.query(models.Student).filter(
            (models.Student.email == data["email"]) |
            (func.lower(models.Student.university_id) == data["university_id"].lower())
        ).with_for_update().all()
        if any(student.email_verified for student in existing):
            raise HTTPException(status_code=409, detail="Email or university ID already registered")
        if len(existing) > 1:
            # Conflicting legacy, unverified records require manual review;
            # never overwrite or discard multiple accounts automatically.
            raise HTTPException(status_code=409, detail="Conflicting unverified accounts; contact support")

        if existing:
            # Allow the verified mailbox owner to reclaim a legacy unverified
            # row that was previously reserving this identity.
            for field, value in data.items():
                setattr(existing[0], field, value)
            existing[0].email_verified = True
            existing[0].verification_token = None
            # A pilot account reclaimed through the normal
            # verification flow becomes a regular verified
            # account; the pilot marker is cleared.
            existing[0].pilot_account = False
        else:
            db.add(models.Student(**data, email_verified=True))
        db.delete(pending)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(status_code=409, detail="Email or university ID already registered")

        return {"message": "Email verified successfully"}
    finally:
        db.close()


@router.post("/login")
def login(student: StudentLogin, request: Request):

    _check_login_rate_limit(request, student.email)

    db = database.SessionLocal()

    try:
        user = db.query(models.Student).filter(
            models.Student.email == student.email.lower()
        ).first()

        if not user or not verify_password(
            student.password, user.password
        ):
            # Count against the submitted mailbox even when no such
            # account exists, so enumeration via response timing or
            # status codes cannot be combined with a free second pass.
            _record_login_failure(request, student.email)
            # No email and no password here: only the id when the
            # account exists, so a log line never becomes a second copy
            # of personal data or a credential oracle.
            log_event(
                "login_failed",
                reason="bad_credentials",
                user_id=user.id if user else None,
                client_ip=_client_ip(request),
            )
            raise HTTPException(
                status_code=401,
                detail="Invalid email or password"
            )

        if not user.email_verified:
            # Supervised pilot policy: a pilot account may sign in
            # only while the pilot is explicitly enabled. When the
            # pilot ends (flag off) it is treated like any other
            # unverified account until the normal email-verification
            # flow completes for that mailbox.
            if user.pilot_account and pilot_onboarding_enabled():
                pass
            elif user.pilot_account:
                log_event(
                    "login_denied",
                    reason="pilot_ended",
                    user_id=user.id,
                )
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "Pilot sign-in has ended. Complete email "
                        "verification to keep using FYP Finder."
                    )
                )
            else:
                log_event(
                    "login_denied",
                    reason="unverified",
                    user_id=user.id,
                )
                raise HTTPException(
                    status_code=403,
                    detail="Please verify your email before logging in"
                )

        token = create_access_token(user.id)

        log_event("login_succeeded", user_id=user.id,
                  client_ip=_client_ip(request))

        return {
            "access_token": token,
            "token_type": "bearer",
            "student_id": user.id
        }
    finally:
        db.close()


@router.post("/forgot-password")
def forgot_password(request: schemas.PasswordResetRequest):
    db = database.SessionLocal()

    try:
        student = db.query(models.Student).filter(
            models.Student.email == request.email.lower(),
            models.Student.email_verified.is_(True),
        ).first()

        if student:
            # Invalidate any previous reset links for this account.
            db.query(models.PasswordResetToken).filter(
                models.PasswordResetToken.student_id == student.id
            ).delete()

            token = secrets.token_urlsafe(32)
            db.add(models.PasswordResetToken(
                token_hash=_token_hash(token),
                student_id=student.id,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            ))
            db.commit()

            try:
                _send_password_reset_email(student.email, token)
            except Exception:
                # Delivery failed. Discard the token and answer with
                # the same generic message used for every request:
                # a delivery-specific error (e.g. 503) would only
                # be reachable for registered accounts and would
                # reveal that the email exists. The student can
                # simply request a new link.
                db.query(models.PasswordResetToken).filter(
                    models.PasswordResetToken.student_id == student.id
                ).delete()
                db.commit()
    finally:
        db.close()

    # Always return the same generic message so account existence is not
    # revealed.
    return {
        "message": "If that email is registered, a password reset link has been sent."
    }


@router.post("/reset-password")
def reset_password(request: schemas.PasswordReset):
    db = database.SessionLocal()

    try:
        row = db.query(models.PasswordResetToken).filter(
            models.PasswordResetToken.token_hash == _token_hash(request.token)
        ).first()

        if not row:
            raise HTTPException(
                status_code=400, detail="Invalid or expired reset link"
            )

        expires_at = row.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        if datetime.now(timezone.utc) >= expires_at:
            db.delete(row)
            db.commit()
            raise HTTPException(
                status_code=400, detail="Invalid or expired reset link"
            )

        student = db.query(models.Student).filter(
            models.Student.id == row.student_id
        ).first()

        if not student:
            raise HTTPException(
                status_code=400, detail="Invalid or expired reset link"
            )

        student.password = hash_password(request.password)
        db.delete(row)
        db.commit()

        return {"message": "Password updated. You can now sign in."}
    finally:
        db.close()
