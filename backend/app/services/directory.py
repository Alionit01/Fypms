"""Student-directory serialization and search helpers (audit SEC-08).

``DIRECTORY_FIELDS`` is the allowlist of columns the directory may expose;
``main.py`` re-exports it because a test asserts the exact key set.
"""

from sqlalchemy import func

from .. import models
from ..schemas import DIRECTORY_PICTURE_INLINE_LIMIT

# The student directory: the only fields other students may see.
# Private columns (password hash, email, whatsapp, email_verified,
# verification_token, profile_completed) are never returned, and raw
# ORM rows are never serialized to responses.
DIRECTORY_FIELDS = (
    "id",
    "name",
    "university_id",
    "program",
    "profile_picture",
    "bio",
    "github",
    "linkedin",
    "skills",
    "interests",
    "fyp_status",
)


def _directory_entry(student: models.Student, include_picture: bool = True) -> dict:
    entry = {field: getattr(student, field) for field in DIRECTORY_FIELDS}
    if not include_picture:
        # SEC-08: a directory page would otherwise ship every avatar
        # inline (observed: 50 students x ~400 KB each). The picture is
        # still served by /students/{id}; the frontend's <Avatar>
        # component shows an initial badge when it is absent.
        picture = entry.get("profile_picture")
        if picture and len(picture) > DIRECTORY_PICTURE_INLINE_LIMIT:
            entry["profile_picture"] = None
    return entry


def _search_pattern(term: str) -> str:
    """Turn a typed term into a case-insensitive `LIKE` pattern.

    % and _ are LIKE's own wildcards, so a student searching for
    "BSCS_1" or "100%" must not silently match everything — they are
    escaped here and the backslash is declared as the escape character
    at the call site. The term is lowercased once so callers can use
    `func.lower(column).like(pattern)` and get the same case-folding
    on every database backend.
    """
    escaped = (
        term.lower()
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{escaped}%"


def _contains(column, pattern: str):
    """One case-insensitive "contains" clause for `_search_pattern`.

    Written as a helper so every directory search folds case the same
    way and honours the same escape character.
    """
    return func.lower(column).like(pattern, escape="\\")
