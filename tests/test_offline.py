import hashlib
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from browse_anything import anubis, cloudflare
from browse_anything.sandbox import confine

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_proof_of_work():
    nonce, digest = anubis.proof_of_work("random", 3)
    assert digest == hashlib.sha256(f"random{nonce}".encode()).hexdigest()
    assert digest.startswith("000")


def test_anubis_fast():
    body = fixture("anubis_fast.html")
    assert anubis.detect(body)
    url, delay = anubis.solve("https://nitter.example/user", body)
    query = parse_qs(urlsplit(url).query)
    assert url.startswith("https://nitter.example/.within.website/x/cmd/anubis/api/pass-challenge?")
    assert query["redir"] == ["https://nitter.example/user"] and delay == 0
    assert query["response"][0].startswith("0000")


@pytest.mark.parametrize("refresh, body", [
    ("2; url=/.within.website/x/cmd/anubis/api/pass-challenge?challenge=abc&id=1", ""),
    (None, '<meta http-equiv="refresh" content="2; url=/.within.website/x/cmd/anubis/api/pass-challenge?challenge=abc&amp;id=1">'),
])
def test_anubis_metarefresh(refresh, body):
    url, delay = anubis.solve("https://anubis.example/", fixture("anubis_metarefresh.html") + body, refresh)
    assert url == "https://anubis.example/.within.website/x/cmd/anubis/api/pass-challenge?challenge=abc&id=1"
    assert delay == 2


def test_cloudflare_detect():
    assert cloudflare.detect({}, fixture("cloudflare.html"))
    assert cloudflare.detect({"cf-mitigated": memoryview(b"challenge")}, "")
    assert not cloudflare.detect({}, fixture("anubis_fast.html"))


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt")
def test_sandbox_confines_writes(tmp_path):
    outside = Path.home() / f".browse-anything-sandbox-test-{os.getpid()}"
    assert subprocess.run(confine(["touch", str(tmp_path / "inside")], str(tmp_path))).returncode == 0
    assert subprocess.run(confine(["touch", str(outside)], str(tmp_path)), stderr=subprocess.DEVNULL).returncode != 0
    assert not outside.exists()


def test_walk_descends_into_shadow_roots_and_frames():
    checkbox = {"nodeName": "INPUT", "attributes": ["type", "checkbox"]}
    iframe = {"nodeName": "IFRAME", "contentDocument": {"nodeName": "#document", "children": [{"nodeName": "DIV", "shadowRoots": [checkbox]}]}}
    found = [attributes for node, attributes in cloudflare.walk({"nodeName": "BODY", "children": [iframe]}) if node["nodeName"] == "INPUT"]
    assert found == [{"type": "checkbox"}]
    assert list(cloudflare.walk(None)) == []
