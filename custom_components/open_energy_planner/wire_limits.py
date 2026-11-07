"""Keep response memory bounded independently of configured appliance and source counts."""

import json

MAX_SNAPSHOT_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_API_RESPONSE_BYTES = 256 * 1024


def validate_projection_size(value, maximum_bytes):
    """Reject oversized decoded projections even when a caller bypasses the HTTP reader."""
    if len(json.dumps(value, allow_nan=False).encode()) > maximum_bytes:
        raise ValueError("App projection exceeds size limit")
