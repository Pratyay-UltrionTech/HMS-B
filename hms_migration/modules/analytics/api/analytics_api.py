"""
HTTP API router for the Analytics module.

Conforms to UltrionTech-Backend-Template modules/analytics/api/ specification.
Thin controller that authenticates super_admin access, resolves dependencies,
delegates to GetPlatformAnalyticsAction, and returns a validated PlatformAnalyticsResponse.
"""

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from hms_migration.infrastructure.postgres.session import (
    get_transitional_sync_session as get_db,
)
from hms_migration.modules.analytics.actions.get_platform_analytics_action import (
    GetPlatformAnalyticsAction,
)
from hms_migration.modules.analytics.contracts.analytics_contracts import (
    PlatformAnalyticsResponse,
)
from hms_migration.modules.analytics.db.analytics_repository import AnalyticsRepository
from hms_migration.shared.auth.dependencies import require_super_admin

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/platform", response_model=PlatformAnalyticsResponse)
def get_platform_analytics(
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require_super_admin),
) -> PlatformAnalyticsResponse:
    """Retrieve platform-wide operational, subscription, and historical growth analytics."""
    repo = AnalyticsRepository(db=db)
    return GetPlatformAnalyticsAction(repo=repo).execute()
