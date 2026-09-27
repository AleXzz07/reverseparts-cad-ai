"""Bind optional DXF checks to an analysis produced by this server process.

An opaque MAC avoids accepting client-modified volume, hole counts or flats.
It is deliberately transient: a restarted server requires a new STEP analysis.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets


_KEY = secrets.token_bytes(32)


def _canonical(value, *, browser_input=False):
    """Encode JSON values by type and numeric value, independent of JS spelling.

    JSON.parse/JSON.stringify preserves the IEEE-754 value of ordinary numbers,
    but changes -0.0 to 0, 2.0 to 2 and sometimes decimal exponent notation.
    Canonical numbers use the same binary value, including zero without sign.
    """
    if value is None:
        return ['null']
    if isinstance(value, bool):
        return ['boolean', value]
    if isinstance(value, int):
        try:
            numeric = float(value)
        except OverflowError as exc:
            raise ValueError('JSON integer cannot round-trip through JavaScript exactly') from exc
        if not math.isfinite(numeric) or (not browser_input and int(numeric) != value):
            raise ValueError('JSON integer cannot round-trip through JavaScript exactly')
        return ['number', numeric.hex()]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError('Non-finite JSON number')
        return ['number', (0.0 if value == 0.0 else value).hex()]
    if isinstance(value, str):
        return ['string', value]
    if isinstance(value, (list, tuple)):
        return ['array', [_canonical(item,browser_input=browser_input) for item in value]]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError('JSON object keys must be strings')
        return ['object', [[key, _canonical(value[key],browser_input=browser_input)] for key in sorted(value)]]
    raise TypeError('Unsupported analysis value')


def analysis_receipt(analysis: dict, *, _browser_input=False) -> str:
    canonical = json.dumps(_canonical(analysis,browser_input=_browser_input), ensure_ascii=False,
                           separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hmac.new(_KEY, canonical, hashlib.sha256).hexdigest()


def valid_analysis_receipt(analysis: dict, receipt: str) -> bool:
    if not isinstance(receipt, str) or not receipt:
        return False
    try:
        return hmac.compare_digest(analysis_receipt(analysis,_browser_input=True), receipt)
    except (TypeError, ValueError):
        return False
