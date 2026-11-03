"""Drain off-loop writes before releasing their application lock, including repeated cancellation."""

import asyncio


async def complete_write(save, *args):
    """Finish one owned writer and return delayed cancellation for propagation after state installation."""
    writer = asyncio.create_task(asyncio.to_thread(save, *args))
    cancellation = None
    while True:
        try:
            await asyncio.shield(writer)
            return cancellation
        except asyncio.CancelledError as error:
            if writer.cancelled():
                raise
            cancellation = error
