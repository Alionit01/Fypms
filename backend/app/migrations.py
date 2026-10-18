"""Versioned, repeatable schema migrations.

Replaces the old inline ``ALTER TABLE`` block that ran at import time
inside a blanket ``try/except Exception: pass``. That pattern had two
real defects (found in the audit):

* SEC-04 — it contained an unguarded bulk ``UPDATE`` that set every
  account's ``email_verified`` to true wherever ``verification_token``
  was null. Nothing writes that token any longer, so it matched *every*
  row on *every* boot, silently converting unverified pilot accounts
  into verified ones.
* DB-02 — failures were swallowed, so a partially applied migration
  looked like a healthy start-up.

Design:

* Migrations are an ordered list of ``(version, description, fn)``.
* Applied versions are recorded in ``schema_migrations`` so a migration
  runs exactly once per database.
* Every migration is written to be **idempotent at the schema level**
  (it inspects the live schema before changing anything), so replaying
  the whole list against a database that already has the shape is safe.
  This matters because the test suite drops and recreates all tables
  between cases.
* A migration that cannot complete **raises**. It never swallows an
  exception and never leaves a half-applied change behind: each
  migration runs inside a transaction that is rolled back on error.

Nothing here deletes, rewrites or reinterprets user data. Where a
constraint cannot be created because existing rows violate it, the
migration refuses to run and prints the offending rows so a human can
reconcile them deliberately.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, String, text
from sqlalchemy.orm import Session

from .database import Base, engine
from .models import INVITATION_TTL


class MigrationError(RuntimeError):
    """A migration could not be applied safely.

    Deliberately a hard failure: operators must see it rather than
    continue running against a schema that does not match the code.
    """


class SchemaMigration(Base):
    """Bookkeeping table for applied migrations."""

    __tablename__ = "schema_migrations"

    version = Column(String(64), primary_key=True)
    description = Column(String, nullable=False)
    applied_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _inspector(engine_):
    from sqlalchemy import inspect

    return inspect(engine_)


def _table_exists(engine_, table: str) -> bool:
    return table in _inspector(engine_).get_table_names()


def _has_column(engine_, table: str, column: str) -> bool:
    return any(
        col["name"] == column
        for col in _inspector(engine_).get_columns(table)
    )


def _add_column(engine_, table: str, ddl: str, *, default_literal=None) -> None:
    """Add ``table.column`` if it is missing (works on SQLite too).

    SQLite cannot ``ADD COLUMN IF NOT EXISTS``, so we ask the schema
    instead of relying on vendor syntax. A table that does not exist at
    all is nothing to migrate: the ORM creates it with its full current
    shape on the next ``create_all``.
    """
    if not _table_exists(engine_, table):
        return
    if _has_column(engine_, table, column := ddl.split()[0]):
        return
    with engine_.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
    if default_literal is not None:
        # Backfill rows that predate the column. This is a narrow,
        # explicit default for one column — never a broad "mark
        # everything true" statement (see SEC-04).
        with engine_.begin() as conn:
            conn.execute(text(
                f"UPDATE {table} SET {column} = :value "
                f"WHERE {column} IS NULL"
            ), {"value": default_literal})


def _unique_index_exists(engine_, table: str, name: str) -> bool:
    return any(
        ix["name"] == name
        for ix in _inspector(engine_).get_indexes(table)
    )


def _index_exists(engine_, table: str, name: str) -> bool:
    """True when any index (unique or not) named ``name`` exists."""
    return any(
        ix["name"] == name
        for ix in _inspector(engine_).get_indexes(table)
    )


def _unique_constraint_exists(engine_, table: str, name: str) -> bool:
    return any(
        uc["name"] == name
        for uc in _inspector(engine_).get_unique_constraints(table)
    )


# ---------------------------------------------------------------------------
# migrations
# ---------------------------------------------------------------------------

def _migration_0001_students_baseline(engine_) -> None:
    """Add the student columns introduced after the first deploy.

    Column-level only, idempotent, and — unlike the code this replaces —
    it performs **no** data rewrite. Email verification state is never
    touched here (see SEC-04).
    """
    _add_column(engine_, "students", "email_verified BOOLEAN NOT NULL DEFAULT FALSE")
    _add_column(engine_, "students", "verification_token VARCHAR")
    # ``profile_completed`` historically defaulted TRUE so that rows
    # created before the feature existed were not locked out. Preserve
    # that intent for pre-existing rows only; new rows are written
    # explicitly by the ORM default (False).
    _add_column(
        engine_, "students", "profile_completed BOOLEAN NOT NULL DEFAULT TRUE",
        default_literal=True,
    )
    _add_column(
        engine_, "students", "role VARCHAR NOT NULL DEFAULT 'student'",
        default_literal="student",
    )
    _add_column(
        engine_, "students", "pilot_account BOOLEAN NOT NULL DEFAULT FALSE",
        default_literal=False,
    )


def _migration_0002_team_members_unique_student(engine_) -> None:
    """DB-01: enforce one team membership per student at the database.

    ``models.TeamMember`` has always declared
    ``UniqueConstraint("student_id", name="unique_student_team")``, but
    ``create_all`` never alters an existing table, so databases created
    before that constraint was added silently lost the guarantee. The
    application-level "already in a team" checks are check-then-insert
    and race when the same student accepts invitations to two different
    teams concurrently (each request locks a *different* team row).

    The database is the authority. If rows already violate the rule we
    refuse to create the constraint and name them, so reconciliation is
    a deliberate human decision instead of a silent data change.
    """
    if _unique_constraint_exists(engine_, "team_members", "unique_student_team"):
        return
    if _unique_index_exists(engine_, "team_members", "unique_student_team"):
        return

    with engine_.connect() as conn_:
        offenders = conn_.execute(text(
            "SELECT student_id, COUNT(*) AS team_count, "
            "STRING_AGG(CAST(team_id AS VARCHAR), ',' ORDER BY team_id) AS team_ids "
            "FROM team_members GROUP BY student_id HAVING COUNT(*) > 1"
        )).fetchall() if engine_.dialect.name == "postgresql" else conn_.execute(text(
            "SELECT student_id, COUNT(*) AS team_count, "
            "GROUP_CONCAT(team_id) AS team_ids "
            "FROM team_members GROUP BY student_id HAVING COUNT(*) > 1"
        )).fetchall()

    if offenders:
        detail = "; ".join(
            f"student_id={row[0]} is in {row[1]} teams ({row[2]})"
            for row in offenders
        )
        raise MigrationError(
            "Cannot create UNIQUE (student_id) on team_members: existing "
            f"rows already violate one-membership-per-student. {detail}. "
            "Reconcile these memberships deliberately, then re-run."
        )

    if engine_.dialect.name == "postgresql":
        ddl = ("ALTER TABLE team_members "
               "ADD CONSTRAINT unique_student_team UNIQUE (student_id)")
    else:
        # SQLite cannot ALTER TABLE ADD CONSTRAINT, but a unique index
        # enforces exactly the same rule.
        ddl = ("CREATE UNIQUE INDEX unique_student_team "
               "ON team_members (student_id)")

    with engine_.begin() as conn_:
        conn_.execute(text(ddl))


def _migration_0003_team_invitation_timestamps(engine_) -> None:
    """Give invitations a sent-at and an expiry (INV-01).

    Invitations used to be a bare (team_id, student_id) row: no sent
    date, no deadline, no way to tell a fresh invite from one sent
    months ago. Both columns are added without one, backfilled with
    "just now" and "just now + INVITATION_TTL" so every existing
    invitation becomes a normal, freshly-sent one instead of appearing
    already expired or being deleted out from under someone.
    """
    _add_column(
        engine_,
        "team_invitations",
        "created_at TIMESTAMP",
        # Naive UTC, matching how the rest of this schema stores
        # timestamps for SQLite (readers attach tzinfo back).
        default_literal=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    _add_column(
        engine_,
        "team_invitations",
        "expires_at TIMESTAMP",
        default_literal=(
            datetime.now(timezone.utc) + INVITATION_TTL
        ).replace(tzinfo=None),
    )


def _migration_0004_team_invitation_indexes(engine_) -> None:
    """Index the invitation lookups the hot path runs on every request.

    ``GET /my-invitations`` — polled by the navbar on every route change —
    filters ``team_invitations`` by ``student_id`` alone. The existing
    composite unique index ``unique_team_invitee`` is on
    (team_id, student_id), whose leading column is team_id, so it cannot
    serve a student_id-only predicate. Measured with EXPLAIN against a
    62k-row stand-in:

        Seq Scan          cost=1351   (no index)
        Bitmap Index Scan cost=23     (with ix_team_invitations_student_id)

    ``_purge_expired_invitations`` filters on ``expires_at <= now()`` and
    likewise had no supporting index.

    Both are created only if absent, so this is idempotent and safe to
    replay (the test suite drops and recreates tables between cases).
    This addresses scan cost only; the dominant production cost is the
    database round-trip itself, which the N+1 query reduction handles.
    """
    if not _table_exists(engine_, "team_invitations"):
        return
    for name, column in (
        ("ix_team_invitations_student_id", "student_id"),
        ("ix_team_invitations_expires_at", "expires_at"),
    ):
        if _index_exists(engine_, "team_invitations", name):
            continue
        with engine_.begin() as conn:
            conn.execute(text(
                f"CREATE INDEX {name} ON team_invitations ({column})"
            ))


MIGRATIONS = [
    ("0001_students_baseline",
     "Add students columns introduced after first deploy (no data rewrite)",
     _migration_0001_students_baseline),
    ("0002_team_members_unique_student",
     "DB-01: enforce one team membership per student",
     _migration_0002_team_members_unique_student),
    ("0003_team_invitation_timestamps",
     "INV-01: record when an invitation was sent and when it expires",
     _migration_0003_team_invitation_timestamps),
    ("0004_team_invitation_indexes",
     "Index team_invitations.student_id and expires_at for the hot path",
     _migration_0004_team_invitation_indexes),
]


def run_migrations(engine_=engine) -> list[str]:
    """Apply pending migrations. Returns the versions applied.

    Raises MigrationError (or the underlying DB error) on failure —
    never swallows an exception.
    """
    # The bookkeeping table is normally part of Base.metadata, so
    # create_all has made it. A migration runner must not *depend* on
    # that though: create it here if it is missing (a legacy database
    # predating the runner has no such table).
    with engine_.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version VARCHAR(64) NOT NULL PRIMARY KEY,"
            " description VARCHAR NOT NULL,"
            " applied_at TIMESTAMP NOT NULL)"
        ))

    with Session(engine_) as session:
        applied = {
            row.version for row in session.execute(
                text("SELECT version FROM schema_migrations")
            ).fetchall()
        }

    ran: list[str] = []
    for version, description, fn in MIGRATIONS:
        if version in applied:
            continue
        try:
            fn(engine_)
        except MigrationError:
            raise
        except Exception as exc:  # noqa: BLE001 - re-raised, never swallowed
            raise MigrationError(
                f"Migration {version} ({description}) failed: {exc}"
            ) from exc

        with engine_.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO schema_migrations (version, description, applied_at) "
                    "VALUES (:version, :description, :applied_at)"
                ),
                {
                    "version": version,
                    "description": description,
                    "applied_at": datetime.now(timezone.utc),
                },
            )
        ran.append(version)

    return ran
