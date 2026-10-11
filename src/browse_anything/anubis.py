"""Solver for Anubis (https://github.com/TecharoHQ/anubis) challenges."""

import hashlib
import html
import json
import re
import time
from itertools import count
from urllib.parse import urlencode, urljoin, urlsplit

_DATA = re.compile(r'<script id="anubis_(challenge|base_prefix)"[^>]*>(.*?)</script>', re.S)
_META_REFRESH = re.compile(r'<meta http-equiv="refresh" content="([^"]+)"', re.I)


def detect(body: str) -> bool:
    return '<script id="anubis_challenge"' in body


def solve(url: str, body: str, refresh: str | None = None) -> tuple[str, float]:
    """Return the URL that redeems the challenge and the seconds to wait before requesting it.

    `refresh` is the response's Refresh header, which carries the target of `metarefresh` challenges.
    """
    data = {key: json.loads(value) for key, value in _DATA.findall(body)}
    rules, challenge = data["challenge"]["rules"], data["challenge"]["challenge"]
    if rules["algorithm"] == "metarefresh":
        refresh = refresh or html.unescape(_META_REFRESH.search(body)[1])
        delay, target = re.fullmatch(r"\s*(\d+)\s*;\s*url=(.+)", refresh, re.I).groups()
        return urljoin(url, target), float(delay)
    if rules["algorithm"] not in ("fast", "slow"):
        raise NotImplementedError(f"Anubis algorithm {rules['algorithm']!r}")

    started = time.monotonic()
    nonce, digest = proof_of_work(challenge["randomData"], rules["difficulty"])
    query = urlencode({
        "id": challenge["id"],
        "response": digest,
        "nonce": nonce,
        "redir": url,
        "elapsedTime": int((time.monotonic() - started) * 1000),
    })
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(url))
    return f"{origin}{data.get('base_prefix', '')}/.within.website/x/cmd/anubis/api/pass-challenge?{query}", 0.0


def proof_of_work(data: str, difficulty: int) -> tuple[int, str]:
    """First nonce whose sha256(data + nonce) hex digest starts with `difficulty` zeros."""
    prefix, base = "0" * difficulty, hashlib.sha256(data.encode())
    for nonce in count():
        hash = base.copy()
        hash.update(str(nonce).encode())
        if (digest := hash.hexdigest()).startswith(prefix):
            return nonce, digest
