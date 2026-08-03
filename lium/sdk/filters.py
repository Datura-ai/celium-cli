"""Client-side node filters shared by the SDK and the CLI.

The web browse page runs the same predicates in
``lium-io-frontend/src/contexts/browseFilterPredicates.ts``. The three subtle ones —
max price (both modes), tier, and minimum reliability — are held to a byte-mirrored
fixture: ``lium/test/fixtures/filter_executors.json`` mirrors
``lium-io-frontend/src/contexts/__fixtures__/filter-executors.json``, and both sides
assert the same accepted-ID sets.
"""

from typing import TYPE_CHECKING, Optional, Sequence

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .models import ExecutorInfo

def _is_splittable_for_count(ex: "ExecutorInfo", wanted: int) -> bool:
    """Return True if *ex* is a splittable node that can serve *wanted* GPUs.

    PREDICATE PARITY: must match matchesGpuCountFilter in
    lium-io-frontend/src/contexts/gpuCountFilter.ts.  Shared fixture:
    lium/test/fixtures/splittable_executors.json mirrored in
    lium-io-frontend/src/contexts/__fixtures__/splittable-executors.json.
    """
    minc = ex.min_gpu_count_for_rental  # None-safe per CLAUDE.md
    avail = ex.available_gpu_count
    if minc is None or avail is None:
        return False
    return minc <= wanted <= avail


def display_gpu_count(ex: "ExecutorInfo", wanted: Optional[int]) -> int:
    """How many GPUs the caller would actually rent, which is what they get priced on.

    Mirrors computeDisplayGpuCount in
    lium-io-frontend/src/modules/pod/browse-pod/displayGpuCount.ts.
    """
    # No fallback to gpu_count: computeDisplayGpuCount returns available_gpu_count
    # outright, and a fallback only this side has would be a silent parity break.
    avail = ex.available_gpu_count or 0
    if wanted and _is_splittable_for_count(ex, wanted):
        return wanted
    return avail


# PREDICATE PARITY: matchesTier in lium-io-frontend/src/contexts/browseFilterPredicates.ts.
def matches_tier(ex: "ExecutorInfo", tier: Optional[str]) -> bool:
    if not tier or tier == "any":
        return True
    return ex.tier == tier


# PREDICATE PARITY: matchesMinReliability in
# lium-io-frontend/src/contexts/browseFilterPredicates.ts.
#
# A missing reliability score means "not enough data yet", not "unreliable", so those
# nodes are kept unless the caller passes include_unscored=False.
def matches_min_reliability(
    ex: "ExecutorInfo",
    minimum: Optional[float],
    include_unscored: bool = True,
) -> bool:
    if not minimum:
        return True
    if ex.reliability_score is None:
        return include_unscored
    return ex.reliability_score >= minimum


# PREDICATE PARITY: matchesMaxPrice in
# lium-io-frontend/src/contexts/browseFilterPredicates.ts.
#
# "total" compares the price the caller would actually pay for the split they asked for,
# not the whole-node price — a $2.50/GPU node rentable one GPU at a time must survive a
# $5 total budget.
def matches_max_price(
    ex: "ExecutorInfo",
    limit: Optional[float],
    mode: str = "total",
    wanted_gpu_count: Optional[int] = None,
) -> bool:
    if not limit:
        return True
    if mode == "gpu":
        return (ex.price_per_gpu or 0) <= limit
    return (ex.price_per_gpu or 0) * display_gpu_count(ex, wanted_gpu_count) <= limit


def matches_min_uptime(ex: "ExecutorInfo", minimum_minutes: Optional[int]) -> bool:
    if not minimum_minutes:
        return True
    if ex.uptime_in_minutes is None:
        return False
    return ex.uptime_in_minutes >= minimum_minutes


def matches_min_vram(ex: "ExecutorInfo", minimum_gb: Optional[float]) -> bool:
    if not minimum_gb:
        return True
    vram = ex.vram_gb
    return vram is not None and vram >= minimum_gb


def matches_min_vram_total(ex: "ExecutorInfo", minimum_gb: Optional[float]) -> bool:
    if not minimum_gb:
        return True
    vram = ex.vram_total_gb
    return vram is not None and vram >= minimum_gb


def matches_min_ports(ex: "ExecutorInfo", minimum: Optional[int]) -> bool:
    if not minimum:
        return True
    if ex.available_port_count is None:
        return False
    return ex.available_port_count >= minimum


def matches_countries(ex: "ExecutorInfo", codes: Optional[Sequence[str]]) -> bool:
    """ISO country codes, matching what the web filter stores and ``lium up --country`` takes."""
    if not codes:
        return True
    code = (ex.location or {}).get("country_code") or ""
    if not code:
        return False
    return code.upper() in {c.upper() for c in codes}
