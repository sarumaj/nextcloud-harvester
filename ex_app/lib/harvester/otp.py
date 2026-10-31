"""One-time codes (TOTP, RFC 6238), as an authenticator app shows them, for a recorded sign-in.

The job's account holds the secret an authenticator would be given: its base32 text (as sites
show it beside the QR code) or the whole otpauth:// link (the QR code's content).
"""

import base64
import hashlib
import hmac
import struct
import time
from urllib.parse import parse_qs, unquote, urlparse

ALGORITHMS = {"SHA1": hashlib.sha1, "SHA256": hashlib.sha256, "SHA512": hashlib.sha512}


def parse(secret: str) -> tuple[bytes, int, int, str]:
    """The key, digits, period and algorithm of a base32 secret or an otpauth:// link;
    ValueError when it is neither."""
    text = secret.strip()
    digits, period, algorithm = 6, 30, "SHA1"
    if text.lower().startswith("otpauth://"):
        query = parse_qs(urlparse(text).query)
        text = unquote(query.get("secret", [""])[0])
        digits = int(query.get("digits", ["6"])[0])
        period = int(query.get("period", ["30"])[0])
        algorithm = query.get("algorithm", ["SHA1"])[0].upper()
    text = "".join(text.split()).upper().rstrip("=")
    if not text or algorithm not in ALGORITHMS or not 6 <= digits <= 10 or period < 1:
        raise ValueError("not a one-time code's secret (base32 text or an otpauth:// link)")
    try:
        key = base64.b32decode(text + "=" * (-len(text) % 8))
    except (ValueError, base64.binascii.Error) as error:
        raise ValueError("the one-time code's secret is not base32 text") from error
    return key, digits, period, algorithm


def code(secret: str, at: float | None = None) -> str:
    """The code for the moment at (now by default)."""
    key, digits, period, algorithm = parse(secret)
    counter = int((time.time() if at is None else at) // period)
    digest = hmac.new(key, struct.pack(">Q", counter), ALGORITHMS[algorithm]).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10**digits).zfill(digits)
