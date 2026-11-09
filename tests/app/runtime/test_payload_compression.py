"""Large JSON blocks retain exact numerical evidence across portable bounded compression."""

import base64
import gzip
import json
from random import Random

import pytest

from open_energy_planner.storage.adapters import payloads


def test_large_payload_is_lossless_deterministic_and_smaller():
    """Repeated interval fields compress while original Unicode, float precision and timestamps remain exact."""
    original = json.dumps(
        {
            "intervals": [
                {"kwh": 0.30000000000000004, "start": "2026-10-25T01:00:00Z", "name": "Zón"}
            ]
            * 200
        },
        ensure_ascii=False,
    )
    encoded = payloads.encode_payload(original)
    assert len(encoded) < len(original.encode()) / 4
    assert encoded.startswith("oep:gzip:1:")
    assert payloads.encode_payload(original) == encoded
    assert payloads.decode_payload(encoded) == original
    assert json.loads(payloads.decode_payload(encoded))["intervals"][0] == {
        "kwh": 0.30000000000000004,
        "start": "2026-10-25T01:00:00Z",
        "name": "Zón",
    }


def test_original_plain_json_and_small_values_remain_readable():
    """Old archives and literal strings resembling an envelope preserve their exact original bytes."""
    for original in ["null", '{"soc":0.30000000000000004}', '"oep:gzip:1:literal"']:
        assert payloads.encode_payload(original) == original
        assert payloads.decode_payload(original) == original


def test_unprofitable_payload_is_not_expanded():
    """Compression and base64 overhead cannot make random printable document contents larger."""
    random = Random(17)
    original = json.dumps("".join(chr(random.randrange(32, 127)) for _ in range(20000)))
    encoded = payloads.encode_payload(original)
    assert len(encoded.encode()) <= len(original.encode())
    assert payloads.decode_payload(encoded) == original


@pytest.mark.parametrize("kind", ["base64", "version", "truncated", "checksum", "trailing"])
def test_corrupt_payload_never_exposes_retained_values(kind):
    """Malformed envelopes are rejected with fixed guidance, including truncated and concatenated gzip data."""
    compressed = gzip.compress(b'{"private-marker":"private-retained-value"}')
    if kind == "truncated":
        compressed = compressed[:-3]
    elif kind == "checksum":
        compressed = compressed[:-8] + b"badcrc!!"
    elif kind == "trailing":
        compressed += gzip.compress(b"second member")
    encoded = "oep:gzip:1:" + base64.b64encode(compressed).decode()
    if kind == "base64":
        encoded += "private-retained-value!"
    elif kind == "version":
        encoded = encoded.replace("gzip:1:", "gzip:2:")
    with pytest.raises(ValueError) as caught:
        payloads.decode_payload(encoded)
    assert str(caught.value) == "Stored JSON compression is invalid or exceeds its decoding bound"
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_compressed_expansion_is_bounded_without_limiting_old_plain_archives(monkeypatch):
    """An expansion bomb is rejected while oversized legitimate plain JSON keeps its historical read contract."""
    monkeypatch.setattr(payloads, "MAX_COMPRESSED_JSON_BYTES", 512)
    original = json.dumps({"intervals": [0] * 1000})
    encoded = "oep:gzip:1:" + base64.b64encode(gzip.compress(original.encode())).decode()
    with pytest.raises(ValueError, match="Stored JSON compression is invalid"):
        payloads.decode_payload(encoded)
    assert payloads.decode_payload(original) == original
    assert payloads.encode_payload(original) == original
