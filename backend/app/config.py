"""Shared configuration: environment flags and validation constants.

Single home for everything ``main.py`` used to read from ``os.getenv``
inline, plus the small static sets the endpoints validate against.
Values are read at call time (as before) except ALLOWED_ORIGINS, which
is fixed at import exactly as it was.
"""

import os

from datetime import timedelta

# Load .env explicitly so the values below do not depend on the
# import order of sibling modules (database/auth) happening to call it.
from dotenv import load_dotenv

load_dotenv()


# Exact origins allowed to call this API. CORS requires exact string
# matches (no wildcards, and "localhost" != "127.0.0.1"), so they are
# listed explicitly rather than inferred.
#
# The deployed frontend is always allowed. Local dev servers are added
# via the ALLOWED_ORIGINS env var (comma-separated), which *extends*
# the production list so a partial local value can never drop it:
#
#   ALLOWED_ORIGINS=http://127.0.0.1:5199,http://localhost:5173
#
# Never add "*" here — the API returns data for signed-in students.
_PRODUCTION_ORIGINS = ("https://fypms-psi.vercel.app",)

# Sensible local defaults when ALLOWED_ORIGINS is unset: Vite's default
# port on both possible hostnames.
_DEFAULT_DEV_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")

_extra_origins = tuple(
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
)

ALLOWED_ORIGINS = list(
    dict.fromkeys(_PRODUCTION_ORIGINS + (_extra_origins or _DEFAULT_DEV_ORIGINS))
)


PROGRAMS = {
    "BSCS",
    "BSAI",
    "BSCB",
    "BSSE",
    "BESE",
}

FYP_STATUSES = {
    "Looking for a team",
    "Already in a team",
}

TEAM_DEPARTMENT_OPTIONS = PROGRAMS | {"Any"}


# Temporary supervised-pilot onboarding. OFF by default and
# fail-closed: a missing, empty, or unrecognized value keeps
# the normal email-verification flow. Enable only for the
# in-person pilot by setting PILOT_ONBOARDING_ENABLED=true.
_PILOT_ON_VALUES = {"true", "1", "yes", "on"}


def pilot_onboarding_enabled() -> bool:
    return (
        os.getenv("PILOT_ONBOARDING_ENABLED", "")
        .strip()
        .lower()
        in _PILOT_ON_VALUES
    )


def _trusted_proxy_count() -> int:
    """How many reverse proxies sit in front of this application.

    0 (the default) means "no proxy is configured": forwarded headers
    are ignored and the direct TCP peer is the login rate-limit
    identity. Set this in deployment to the number of hops you actually
    operate. Unparseable or negative values fail safe to 0.
    """
    try:
        return max(0, int(os.getenv("TRUSTED_PROXY_COUNT", "0")))
    except ValueError:
        return 0


VERIFICATION_TTL = timedelta(hours=1)
