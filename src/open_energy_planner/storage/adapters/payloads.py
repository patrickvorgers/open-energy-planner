"""Encode large retained JSON losslessly while keeping original plain-text archives readable."""

import base64
import binascii
import gzip
import zlib

COMPRESSED_PREFIX = "oep:gzip:1:"
COMPRESSION_MIN_BYTES = 4096
MAX_COMPRESSED_JSON_BYTES = 64 * 1024 * 1024
COMPRESSION_LEVEL = 1


def encode_payload(payload: str) -> str:
    """Compress only profitable JSON blocks within the bounded decoder's byte allowance.

    Input is already serialized JSON. Hashing and numerical representation remain
    owned by the caller and always refer to the original uncompressed content.
    Base64 keeps the envelope portable across the existing SQL text columns.
    Oversized or small originals remain plain text without introducing a new
    limit on existing readable archives.
    """
    original = payload.encode("utf-8")
    if not COMPRESSION_MIN_BYTES <= len(original) <= MAX_COMPRESSED_JSON_BYTES:
        return payload
    encoded = COMPRESSED_PREFIX + base64.b64encode(
        gzip.compress(original, compresslevel=COMPRESSION_LEVEL, mtime=0)
    ).decode("ascii")
    return encoded if len(encoded) < len(original) else payload


def decode_payload(payload: str) -> str:
    """Restore exact JSON text or reject a corrupt, oversized or unsupported compression envelope.

    Decompression is bounded before allocating the complete output. Gzip checksum,
    end marker and trailing data are verified. Arbitrary retained contents never
    become an exception explanation. Plain historical JSON is returned unchanged.
    """
    if not payload.startswith("oep:gzip:"):
        return payload
    try:
        if not payload.startswith(COMPRESSED_PREFIX) or len(payload) > MAX_COMPRESSED_JSON_BYTES:
            raise ValueError
        compressed = base64.b64decode(payload[len(COMPRESSED_PREFIX) :], validate=True)
        inflater = zlib.decompressobj(wbits=31)
        original = inflater.decompress(compressed, MAX_COMPRESSED_JSON_BYTES + 1)
        if (
            len(original) > MAX_COMPRESSED_JSON_BYTES
            or not inflater.eof
            or inflater.unused_data
            or inflater.unconsumed_tail
        ):
            raise ValueError
        return original.decode("utf-8")
    except (ValueError, UnicodeError, binascii.Error, zlib.error):
        raise ValueError(
            "Stored JSON compression is invalid or exceeds its decoding bound"
        ) from None
