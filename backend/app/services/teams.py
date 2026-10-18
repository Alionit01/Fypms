"""Team-capacity bookkeeping shared by the team endpoints."""

from .. import models
from ..schemas import MAX_TEAM_MEMBERS


def _clamp_spots_available(db, team) -> None:
    """Keep spots_available consistent with the actual member count."""
    db.flush()  # ensure pending add/delete of memberships is counted
    member_count = db.query(models.TeamMember).filter(
        models.TeamMember.team_id == team.id
    ).count()
    team.spots_available = max(
        0,
        min(team.spots_available, MAX_TEAM_MEMBERS - member_count)
    )
