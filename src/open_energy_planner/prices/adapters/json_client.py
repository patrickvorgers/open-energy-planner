"""Shared bounded HTTP client used by composition in each provider adapter."""

import json

from aiohttp import ClientError, ClientTimeout

from ..model import PricesUnavailable

HTTP_TIMEOUT_SECONDS = 20
MAX_RESPONSE_BYTES = 2_000_000
READ_CHUNK_BYTES = 65_536


class JsonClient:
    """Bound public JSON retrieval and refuse redirects without provider authentication."""

    def __init__(self, session):
        """Borrow the runtime-owned asynchronous HTTP session without owning its lifecycle."""
        self.session = session

    async def get(self, url, params):
        """Return bounded object JSON or a sanitized source failure, respecting HTTP status."""
        return await self._request("get", url, params=params)

    async def post(self, url, body):
        """Send bounded public JSON-RPC without redirects or ambient authentication."""
        return await self._request(
            "post", url, json=body, headers={"Accept": "application/json, text/event-stream"}
        )

    async def _request(self, method, url, **options):
        """Share transport limits and sanitized failures across provider request methods."""
        try:
            async with getattr(self.session, method)(
                url,
                **options,
                allow_redirects=False,
                timeout=ClientTimeout(total=HTTP_TIMEOUT_SECONDS),
            ) as response:
                if response.status != 200:
                    raise PricesUnavailable(f"price_http_{response.status}")
                raw = bytearray()
                async for chunk in response.content.iter_chunked(READ_CHUNK_BYTES):
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise PricesUnavailable("price_response_too_large")
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise TypeError("Expected a price object")
                return data
        except PricesUnavailable:
            raise
        except (ClientError, TimeoutError, ValueError, TypeError) as error:
            raise PricesUnavailable("price_response_unavailable") from error
