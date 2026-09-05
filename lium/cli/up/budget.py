"""`lium up --budget`: a dollar cap expressed as the removal time it implies.

The API caps a rental by time only (`removal_scheduled_at`). A budget is turned
into that: at `price_per_hour`, `budget_usd` lasts `budget / price` hours from the
moment billing started, and the pod is scheduled for removal then — the same
mechanism as `--ttl`, so `lium schedules` shows it and `lium schedules rm` cancels it.
"""

from datetime import datetime, timezone
from typing import Optional

from lium.sdk import PodInfo
from lium.sdk.utils import parse_api_timestamp, spend_cap_deadline

# A budget that buys less than this is almost certainly a typo (a pod takes
# minutes to become usable), so it is refused before anything is rented.
MIN_BUDGET_MINUTES = 5


def budget_hours(budget_usd: float, price_per_hour: Optional[float]) -> Optional[float]:
    """How long ``budget_usd`` lasts at ``price_per_hour``; None when the price is unknown."""
    if not price_per_hour or price_per_hour <= 0:
        return None
    return budget_usd / price_per_hour


def budget_deadline(
    pod: PodInfo,
    budget_usd: float,
    *,
    fallback_price: Optional[float] = None,
    now: Optional[datetime] = None,
) -> Optional[datetime]:
    """When the running pod will have spent ``budget_usd``.

    Anchored on the pod's ``created_at`` (billing starts there), or on ``now``
    when the API did not send one. The price is the pod's own; ``fallback_price``
    (the executor's price seen before renting) covers a pod record without one.
    None when no price is known at all.
    """
    price = pod.executor.price_per_hour if pod.executor and pod.executor.price_per_hour else fallback_price
    if not price or price <= 0:
        return None
    started_at = parse_api_timestamp(pod.created_at) or now or datetime.now(timezone.utc)
    return spend_cap_deadline(started_at, price, budget_usd)
