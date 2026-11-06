"""Recognize bot-wall interstitials and find Cloudflare Turnstile's checkbox so it can be clicked like a person would."""

import contextlib
import itertools

from .cdp import ProtocolError

# Main-world check of the page for a challenge interstitial; returns its kind, or null on a regular page.
INTERSTITIAL = r"""(() => {
    if (location.href === 'about:blank') return 'blank';
    if (window._cf_chl_opt) return 'Cloudflare';
    if (document.getElementById('anubis_challenge')) return 'Anubis';
    if (/^(Just a moment\.\.\.|Checking your browser|Vercel Security Checkpoint|DDoS-Guard|Making sure you('|’)re not a bot)/i.test(document.title)) return document.title;
    return document.readyState === 'complete' ? null : 'loading';
})()"""


def detect(headers, body: str) -> bool:
    mitigated = headers.get("cf-mitigated")
    return (mitigated is not None and bytes(mitigated) == b"challenge") or "window._cf_chl_opt" in body


def walk(node: dict):
    """Every node under `node`, descending into shadow roots and same-process frames."""
    nodes = [node] if node else []
    while nodes:
        node = nodes.pop()
        yield node, dict(itertools.batched(node.get("attributes", []), 2))
        nodes += node.get("children", []) + node.get("shadowRoots", []) + ([node["contentDocument"]] if "contentDocument" in node else [])


async def find_turnstile(cdp, session: str, frames: dict[str, str]) -> tuple[float, float] | None:
    """Viewport coordinates of the Turnstile checkbox in the page `session`, once it is rendered and visible.

    `frames` maps out-of-process iframes' frame ids to their sessions.
    """
    root = (await cdp.send("DOM.getDocument", session, depth=-1, pierce=True))["root"]
    iframe = next((node for node, attributes in walk(root)
                   if node["nodeName"] == "IFRAME" and "challenges.cloudflare.com" in attributes.get("src", "")), None)
    if iframe is None:
        return None
    try:
        x, y, *_ = (await cdp.send("DOM.getBoxModel", session, backendNodeId=iframe["backendNodeId"]))["model"]["content"]
    except ProtocolError:  # Not rendered.
        return None
    # The checkbox is in Turnstile's frame: inlined in `root` if it shares the page's process, else in its own session.
    if frame := frames.get(iframe.get("frameId")):
        document, offset = (await cdp.send("DOM.getDocument", frame, depth=-1, pierce=True))["root"], (x, y)
    else:
        frame, document, offset = session, iframe.get("contentDocument"), (0, 0)
    checkbox = next((node for node, attributes in walk(document)
                     if node["nodeName"] == "INPUT" and attributes.get("type") == "checkbox"), None)
    if checkbox is None:
        return None
    with contextlib.suppress(ProtocolError):  # Not rendered yet.
        quad = (await cdp.send("DOM.getBoxModel", frame, backendNodeId=checkbox["backendNodeId"]))["model"]["content"]
        if quad[2] > quad[0]:
            return offset[0] + (quad[0] + quad[4]) / 2, offset[1] + (quad[1] + quad[5]) / 2
    return None
