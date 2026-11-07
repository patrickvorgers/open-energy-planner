"""Bound each native Recorder query while retaining every configured statistics channel."""

RECORDER_CHANNEL_BATCH_SIZE = 64


def channel_batches(entities):
    """Yield deterministic channel batches without imposing a total installation-size limit."""
    ordered = sorted(set(entities))
    for offset in range(0, len(ordered), RECORDER_CHANNEL_BATCH_SIZE):
        yield ordered[offset : offset + RECORDER_CHANNEL_BATCH_SIZE]
