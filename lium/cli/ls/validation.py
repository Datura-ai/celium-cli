"""Validation logic for ls command."""


def validate(
    limit: int | None,
    lat: float | None,
    lon: float | None,
    max_distance: int | None,
    min_cuda_version: float | None = None,
    min_download_mbps: float | None = None,
) -> tuple[bool, str | None]:
    """Validate ls command options, returns (is_valid, error_message)."""

    if min_download_mbps is not None and not min_download_mbps > 0:
        return False, "--min-download must be a positive number of Mbps"

    # Validate limit
    if limit is not None and limit <= 0:
        return False, "Limit must be a positive integer"

    if (lat is None) ^ (lon is None):
        return False, "Both --lat and --lon must be provided together"

    if max_distance is not None and max_distance <= 0:
        return False, "--max-distance must be positive"

    if max_distance is not None and (lat is None or lon is None):
        return False, "--max-distance requires --lat and --lon"

    if min_cuda_version is not None and min_cuda_version <= 0:
        return False, "--min-cuda must be positive"

    return True, None
