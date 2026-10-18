"""Service-liveness and pilot-flag endpoints."""

from fastapi import APIRouter

from ..config import pilot_onboarding_enabled

router = APIRouter()


@router.get("/")
def root():
    return {"message": "FYP Finder API is running!"}


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/pilot-status")
def pilot_status():
    # Public, non-secret flag so the frontend can explain the
    # pilot registration flow. Reveals nothing about accounts.
    return {"pilot_onboarding": pilot_onboarding_enabled()}
