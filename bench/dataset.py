"""The synthetic benchmark dataset, as functions of a seed: one seed always gives the same data.

100,000 links with random 6-character base62 codes, owned by 100 users. The target URLs are made
up (`https://example.com/page/<n>`): a redirect never fetches its target, so they need not be live.
"""

import hashlib
import random

from api.services.codes import ALPHABET, CODE_LENGTH

DEFAULT_SEED = 2026
DEFAULT_LINKS = 100_000
DEFAULT_USERS = 100


def make_codes(count: int = DEFAULT_LINKS, seed: int = DEFAULT_SEED) -> list[str]:
    """`count` different codes. Built from `random()` only, because Python promises that stays
    the same between versions (helpers such as `choices` are not promised)."""
    rng = random.Random(seed)
    seen: set[str] = set()
    codes: list[str] = []
    while len(codes) < count:
        code = "".join(ALPHABET[int(rng.random() * len(ALPHABET))] for _ in range(CODE_LENGTH))
        if code not in seen:
            seen.add(code)
            codes.append(code)
    return codes


def long_url(index: int) -> str:
    return f"https://example.com/page/{index}"


def owner(index: int, users: int = DEFAULT_USERS) -> int:
    """The user id (1-based) that owns link number `index`."""
    return index % users + 1


def fingerprint(codes: list[str]) -> str:
    """A short hash that identifies a dataset, to check that two runs used the same one."""
    return hashlib.sha256("\n".join(codes).encode()).hexdigest()[:16]
