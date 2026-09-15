"""Rules version: a short hash that changes when the rule code or any profile constant changes.

A posting screened under an older rules version is re-screened on the next run.
"""
import hashlib

from backend import profile as P

RULES_CODE_VERSION = "2026-09-15.7"   # bump whenever rules.py logic changes without a constant change


def _stable(value):
    """A repr-stable form: dict keys and set members sorted, so hash order never leaks in."""
    if isinstance(value, dict):
        return sorted((str(k), _stable(v)) for k, v in value.items())
    if isinstance(value, (set, frozenset)):
        return sorted((_stable(v) for v in value), key=repr)
    if isinstance(value, (list, tuple)):
        return [_stable(v) for v in value]
    return value


def profile_constants() -> dict:
    """Every uppercase name in backend.profile (personal overrides included via its star import)."""
    return {k: v for k, v in vars(P).items() if k.isupper()}


def rules_version() -> str:
    """sha1 of RULES_CODE_VERSION + repr(sorted uppercase constants of backend.profile) [:12]."""
    payload = RULES_CODE_VERSION + repr(sorted((k, _stable(v)) for k, v in profile_constants().items()))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
