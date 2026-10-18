"""Login rate limiting (audit SEC-01/SEC-03).

Two limits, both per-mailbox-backed: a composite (client IP, mailbox)
key that stops one source hammering one account, and a mailbox-only key
that stops the same attack spread across many source addresses.

The state lives in module-level dicts, exactly as it did in ``main.py``:
this is a single-process limiter. ``main.py`` re-exports these objects
because the test suite reaches in and clears them between cases.
"""

import time
from collections import deque

from fastapi import HTTPException, Request

from ..observability import log_event
from ..config import _trusted_proxy_count

_LOGIN_ATTEMPTS: dict = {}          # keyed by "<client-ip>|<email>"
_LOGIN_EMAIL_ATTEMPTS: dict = {}    # keyed by "<email>"
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 60
# Per-account ceiling. Higher than the per-client limit on purpose: it
# must survive a distributed source (many client IPs, one target
# mailbox) without letting one attacker lock a student out of their own
# account from a single IP.
LOGIN_EMAIL_MAX_ATTEMPTS = 20
LOGIN_EMAIL_WINDOW_SECONDS = 900
_MAX_TRACKED_KEYS = 10000


def _direct_peer(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _client_ip(request: Request) -> str:
    """Best-effort client address, spoof-resistant by default.

    ``X-Forwarded-For`` is a *claim* written by whoever sent it: proxies
    append the address they observed, so the left-most entries are
    whatever the caller chose to send. The old implementation took the
    first hop, which let an attacker rotate the header on every request
    and reset the login limiter each time (audit SEC-01).

    We therefore only honour the header when a proxy is explicitly
    configured, and we read it from the right. With N trusted proxies
    the right-most N entries were appended by infrastructure we control,
    so the entry at position ``len(hops) - N`` is the real client and
    everything left of it is discarded. With N=0 the header is ignored
    entirely.
    """
    trusted = _trusted_proxy_count()
    if trusted <= 0:
        return _direct_peer(request)

    forwarded = request.headers.get("x-forwarded-for")
    if not forwarded:
        return _direct_peer(request)

    hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
    if len(hops) < trusted:
        # Fewer hops than we trust: the chain is malformed or
        # client-supplied. Do not guess — fall back to the peer.
        return _direct_peer(request)
    return hops[len(hops) - trusted]


def _prune(attempts, window: float, now: float) -> None:
    while attempts and now - attempts[0] > window:
        attempts.popleft()


def _evict(store: dict, window: float) -> None:
    """Bound memory by dropping keys whose newest attempt has expired."""
    if len(store) <= _MAX_TRACKED_KEYS:
        return
    now = time.monotonic()
    for key in [k for k, v in store.items()
                if not v or now - v[-1] > window]:
        del store[key]


def _login_rate_keys(request: Request, email: str) -> list:
    """Both limits a failed login counts against.

    The composite (client, mailbox) key is the primary defence and stops
    a single source hammering one account. The mailbox-only key stops
    the same attack spread across many source addresses.
    """
    email = (email or "").strip().lower()
    return [
        (_LOGIN_ATTEMPTS, f"{_client_ip(request)}|{email}",
         LOGIN_WINDOW_SECONDS, LOGIN_MAX_ATTEMPTS),
        (_LOGIN_EMAIL_ATTEMPTS, email,
         LOGIN_EMAIL_WINDOW_SECONDS, LOGIN_EMAIL_MAX_ATTEMPTS),
    ]


def _record_login_failure(request: Request, email: str) -> None:
    now = time.monotonic()
    for store, key, window, _limit in _login_rate_keys(request, email):
        attempts = store.setdefault(key, deque())
        _prune(attempts, window, now)
        attempts.append(now)
    _evict(_LOGIN_ATTEMPTS, LOGIN_WINDOW_SECONDS)
    _evict(_LOGIN_EMAIL_ATTEMPTS, LOGIN_EMAIL_WINDOW_SECONDS)


def _check_login_rate_limit(request: Request, email: str) -> None:
    now = time.monotonic()
    for store, key, window, limit in _login_rate_keys(request, email):
        attempts = store.get(key)
        if not attempts:
            continue
        _prune(attempts, window, now)
        if len(attempts) >= limit:
            log_event(
                "login_rate_limited",
                client_ip=_client_ip(request),
                scope=("account" if store is _LOGIN_EMAIL_ATTEMPTS
                       else "client"),
                limit=limit,
                window_seconds=int(window),
            )
            raise HTTPException(
                status_code=429,
                detail="Too many login attempts. Please try again later."
            )
