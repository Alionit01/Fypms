"""FastAPI application: wiring, startup, middleware, and routers.

The endpoint handlers live in ``app.routers``; shared helpers live in
``app.services`` and ``app.config``. This module keeps exactly what must
run at import time (logging, table creation, migrations) plus the names
the test suite reaches for through ``main.<name>`` — see the
compatibility re-exports at the bottom.
"""

import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import database, models  # noqa: F401  (models registers tables)
from .config import (
    ALLOWED_ORIGINS,
    FYP_STATUSES,
    PROGRAMS,
    TEAM_DEPARTMENT_OPTIONS,
    _trusted_proxy_count,
    pilot_onboarding_enabled,
)
from .database import Base, engine
from .observability import log_event, log_warning

# Load .env explicitly so ALLOWED_ORIGINS above does not depend on the
# import order of sibling modules (database/auth) happening to call it.
load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Create database tables, then apply versioned migrations.
#
# This replaces an inline ALTER TABLE block that ran inside
# ``try/except Exception: pass``. That block performed an unguarded
# ``UPDATE students SET email_verified = TRUE WHERE verification_token
# = NULL`` on *every* start-up in every worker. Because nothing writes
# ``verification_token`` any longer, it matched every row and silently
# converted unverified pilot accounts into verified ones (audit finding
# SEC-04). Migration failure is now loud instead of swallowed (DB-02).
#
# Imported above create_all so that SchemaMigration is registered in
# Base.metadata and its bookkeeping table is created with the rest.
from .migrations import run_migrations  # noqa: E402
from .observability import configure as configure_logging  # noqa: E402

configure_logging()

Base.metadata.create_all(bind=engine)
try:
    applied = run_migrations(engine)
except Exception:
    # DB-02: a migration that cannot complete must be loud. Record it
    # with its traceback before the process exits.
    from .observability import log_exception

    log_exception("migration_failed", phase="startup")
    raise

if applied:
    # Migrations are rare and consequential; record which ones ran.
    log_event("migrations_applied", versions=",".join(applied))


log_event(
    "rate_limit_config",
    trusted_proxy_count=_trusted_proxy_count(),
    forwarded_headers="trusted; read right-to-left"
    if _trusted_proxy_count() > 0
    else "IGNORED; the direct peer is used",
)
if _trusted_proxy_count() == 0:
    log_warning(
        "proxy_trust_unconfigured",
        detail=(
            "TRUSTED_PROXY_COUNT is 0, so X-Forwarded-For is ignored. If "
            "this app runs behind a proxy, the ASGI server must not "
            "rewrite the client address either: run uvicorn with "
            "--no-proxy-headers (or an empty FORWARDED_ALLOW_IPS), "
            "otherwise request.client.host is already a client-controlled "
            "claim and the limiter is bypassable."
        ),
    )


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
    )
    return response


# ---------------------------------------------------------------------------
# Error visibility (audit REL-04)
# ---------------------------------------------------------------------------
#
# Before this, an unexpected failure produced a 500 for the client and
# left nothing behind: no traceback, no path, no way to find out what
# happened. The handlers below record the event and preserve the
# response the framework would have sent anyway.


@app.exception_handler(HTTPException)
async def _logged_http_exception(request: Request, exc: HTTPException):
    """Record authorization denials and server-side failures.

    401 and 422 are deliberately not logged: they are routine (a wrong
    password, a rejected form) and are already recorded where they
    matter, so logging them here would add noise and duplicate personal
    data for no benefit.
    """
    if exc.status_code == 403 or exc.status_code >= 500:
        log_event(
            "http_error",
            status=exc.status_code,
            method=request.method,
            path=request.url.path,
        )
    return await http_exception_handler(request, exc)


@app.exception_handler(Exception)
async def _logged_unexpected_exception(request: Request, exc: Exception):
    """Last resort: an exception nothing else handled."""
    from .observability import log_exception

    log_exception(
        "unhandled_exception",
        method=request.method,
        path=request.url.path,
        error_type=type(exc).__name__,
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


# ---------------------------------------------------------------------------
# Routers, registered in the order the routes were originally declared on
# the app so route matching is unchanged (e.g. /teams/{id}/members/me
# must come before /teams/{id}/members/{student_id}).
#
# The routes are copied onto the app instead of using
# ``app.include_router``: FastAPI 0.141 defers included routers behind an
# ``_IncludedRouter`` wrapper that exposes neither ``.path`` nor
# ``.methods``, while the test suite introspects ``main.app.routes``
# directly. With no router-level prefix or dependencies, copying each
# router's APIRoute objects is exactly what include_router would add.
# ---------------------------------------------------------------------------
from .routers import auth, invitations, students, system, teams  # noqa: E402

for _router in (system.router, auth.router, students.router,
                teams.router, invitations.router):
    app.routes.extend(_router.routes)


# ---------------------------------------------------------------------------
# Compatibility re-exports
# ---------------------------------------------------------------------------
#
# The test suite imports these names from ``app.main`` and, in several
# cases, patches them there (send_verification_email,
# send_password_reset_email) or clears shared state through them
# (_LOGIN_ATTEMPTS, _LOGIN_EMAIL_ATTEMPTS). Endpoints moved into
# ``app.routers`` still honour those patches because the auth router
# resolves the mailers through this module at call time.

from .email import send_password_reset_email, send_verification_email  # noqa: E402,F401
from .routers.auth import (  # noqa: E402,F401
    create_student,
    forgot_password,
    login,
    reset_password,
    verify_email,
)
from .routers.invitations import (  # noqa: E402,F401
    accept_invitation,
    decline_invitation,
    get_my_invitations,
    invitation_candidates,
    invite_team_member,
    list_team_invitations,
    resend_team_invitation,
    revoke_team_invitation,
)
from .routers.students import (  # noqa: E402,F401
    get_my_profile,
    get_student,
    get_student_contact,
    get_students,
    update_my_profile,
)
from .routers.system import health, pilot_status, root  # noqa: E402,F401
from .routers.teams import (  # noqa: E402,F401
    create_team,
    delete_team,
    get_my_team,
    get_team,
    get_team_contact,
    get_team_members,
    get_teams,
    leave_team,
    remove_team_member,
    update_team,
)
from .services.contacts import (  # noqa: E402,F401
    _may_view_contact,
    _may_view_team_contact,
)
from .services.directory import DIRECTORY_FIELDS  # noqa: E402,F401
from .services.rate_limit import (  # noqa: E402,F401
    LOGIN_EMAIL_MAX_ATTEMPTS,
    LOGIN_MAX_ATTEMPTS,
    LOGIN_WINDOW_SECONDS,
    _LOGIN_ATTEMPTS,
    _LOGIN_EMAIL_ATTEMPTS,
    _client_ip,
)
