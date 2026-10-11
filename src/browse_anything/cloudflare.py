"""Pass Cloudflare challenges in a real, sandboxed Chrome driven over the DevTools pipe, and render the page.

Chrome runs in new headless mode, which is the full browser without a window. Its user agent is set to the
regular Chrome one, because Cloudflare blocks the `HeadlessChrome` default.
"""

import asyncio
import atexit
import contextlib
import fcntl
import itertools
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .sandbox import confine

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
]

CHROME_FLAGS = [
    "--remote-debugging-pipe",
    "--no-startup-window",
    "--no-first-run",
    "--no-default-browser-check",
    "--use-mock-keychain",
    "--password-store=basic",
    "--no-sandbox",  # Chrome's own sandbox cannot nest inside sandbox-exec or bwrap; the outer one confines it.
    "--disable-blink-features=AutomationControlled",
    "--headless=new",
]

PLATFORMS = {"darwin": "Macintosh; Intel Mac OS X 10_15_7", "linux": "X11; Linux x86_64"}

POLL = 0.25  # Seconds between checks of the challenge's progress.
RECLICK = 4  # Seconds to let a click verify before clicking a checkbox that is still shown.
SCROLL_STEP, SCROLL_PAUSE, SCROLL_MAX = 600, 0.15, 20_000  # Pixels and seconds; scrolling loads lazy content.
IDLE = 1  # Seconds without network requests after which the page counts as rendered.
SETTLE = 10  # Most seconds spent scrolling and waiting for the network once the page has loaded.

# The page's HTML including open shadow roots, as declarative `<template shadowrootmode>`, since `outerHTML` drops
# them and web-component sites keep their content there.
SERIALIZE = """(() => {
    const roots = [];
    const collect = root => root.querySelectorAll('*').forEach(e => { if (e.shadowRoot) { roots.push(e.shadowRoot); collect(e.shadowRoot); } });
    collect(document);
    const html = document.documentElement;
    return '<!DOCTYPE html>' + (html.getHTML ? html.getHTML({shadowRoots: roots}) : html.outerHTML);
})()"""


def detect(headers, body: str) -> bool:
    mitigated = headers.get("cf-mitigated")
    return (mitigated is not None and bytes(mitigated) == b"challenge") or "window._cf_chl_opt" in body


def find_chrome() -> str:
    for candidate in [os.environ.get("BROWSE_ANYTHING_CHROME"), *CHROME_CANDIDATES]:
        if candidate and (path := shutil.which(candidate) or (os.path.isfile(candidate) and candidate)):
            return path
    raise RuntimeError("Chrome not found; set BROWSE_ANYTHING_CHROME")


def user_agent(chrome: str) -> str:
    """The user agent a regular window of this Chrome sends, in Chrome's reduced format."""
    version = subprocess.run([chrome, "--version"], capture_output=True, text=True, check=True).stdout
    major = re.search(r"(\d+)\.\d+\.\d+\.\d+", version)[1]
    platform = PLATFORMS["linux" if sys.platform.startswith("linux") else sys.platform]
    return f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"


def walk(node: dict):
    """Every node under `node`, descending into shadow roots and same-process frames."""
    nodes = [node] if node else []
    while nodes:
        node = nodes.pop()
        yield node, dict(itertools.batched(node.get("attributes", []), 2))
        nodes += node.get("children", []) + node.get("shadowRoots", []) + ([node["contentDocument"]] if "contentDocument" in node else [])


@dataclass
class Rendered:
    url: str
    status: int
    html: str
    cookies: list[dict]
    user_agent: str


@dataclass
class Load:
    """The network activity of a tab: requests in flight and the status of its latest document."""

    frame: str
    status: int = 200
    requests: set[str] = field(default_factory=set)


class Chrome:
    def __init__(self):
        self.profile = tempfile.mkdtemp(prefix="browse-anything-")
        self.process: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.ids = itertools.count(1)
        self.pending: dict[int, asyncio.Future] = {}
        self.frames: defaultdict[str, dict[str, str]] = defaultdict(dict)  # Session -> its out-of-process iframes' frame ids -> sessions.
        self.loads: dict[str, Load] = {}  # Session -> its tab's network activity.
        atexit.register(self.close)

    async def solve(self, url: str, timeout: float = 45) -> Rendered:
        """Load `url`, passing any Cloudflare challenge, and return the rendered page with the site's cookies.

        Once the page has loaded, it is scrolled to the bottom to trigger lazy content and given time for the
        network to go quiet, so content that JavaScript adds is included.
        """
        async with self.lock:
            if self.process is None or self.process.returncode is not None:
                await self.start()
            target = (await self.send("Target.createTarget", url="about:blank", newWindow=True, width=1280, height=900))["targetId"]
            session = None
            try:
                session = (await self.send("Target.attachToTarget", targetId=target, flatten=True))["sessionId"]
                self.loads[session] = Load(frame=target)  # A tab's main frame has its target's id.
                # Out-of-process iframes, such as Turnstile's, get their own sessions so their DOM can be inspected.
                await self.send("Target.setAutoAttach", session, autoAttach=True, waitForDebuggerOnStart=False, flatten=True)
                await self.send("Network.enable", session)
                await self.send("Page.navigate", session, url=url)
                try:
                    await asyncio.wait_for(self.pass_challenge(session), timeout)
                except TimeoutError:
                    raise TimeoutError(f"{url} did not load past Cloudflare in {timeout:.0f}s") from None
                html, final = await self.settle(session)
                return Rendered(final, self.loads[session].status, html, await self.cookies(urlsplit(final).hostname),
                                (await self.send("Browser.getVersion"))["userAgent"])
            finally:
                self.loads.pop(session, None)
                self.frames.pop(session, None)
                with contextlib.suppress(Exception):
                    await self.send("Target.closeTarget", targetId=target)

    async def pass_challenge(self, session: str):
        """Wait until the page has loaded without a challenge, clicking Turnstile's checkbox when it shows."""
        clicked = -RECLICK
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(POLL)
            try:
                if await self.evaluate(session, "location.href != 'about:blank' && document.readyState == 'complete' && !window._cf_chl_opt"):
                    return
            except (RuntimeError, asyncio.TimeoutError):  # Mid-navigation.
                continue
            if loop.time() - clicked > RECLICK and (checkbox := await self.find_turnstile(session)):
                await self.click(session, *checkbox)
                clicked = loop.time()

    async def settle(self, session: str) -> tuple[str, str]:
        """Scroll through the loaded page and wait for its network to go quiet; return its HTML and URL."""
        load, loop = self.loads[session], asyncio.get_running_loop()
        deadline = loop.time() + SETTLE
        with contextlib.suppress(RuntimeError, asyncio.TimeoutError):  # A navigation cut scrolling short.
            y = 0
            while y < min(SCROLL_MAX, await self.evaluate(session, "document.documentElement.scrollHeight")) and loop.time() < deadline:
                y += SCROLL_STEP
                await self.evaluate(session, f"window.scrollTo(0, {y})")
                await asyncio.sleep(SCROLL_PAUSE)
        quiet = loop.time()
        while loop.time() < deadline and loop.time() - quiet < IDLE:
            await asyncio.sleep(0.1)
            if load.requests:
                quiet = loop.time()
        while True:
            try:
                return await self.evaluate(session, SERIALIZE), await self.evaluate(session, "location.href")
            except RuntimeError:  # Mid-navigation.
                await asyncio.sleep(POLL)

    async def cookies(self, host: str) -> list[dict]:
        return [c for c in (await self.send("Storage.getCookies"))["cookies"] if f".{host}".endswith("." + c["domain"].lstrip("."))]

    async def find_turnstile(self, session: str) -> tuple[float, float] | None:
        """Page coordinates of the Turnstile checkbox, once it is rendered and visible."""
        root = (await self.send("DOM.getDocument", session, depth=-1, pierce=True))["root"]
        iframe = next((node for node, attributes in walk(root)
                       if node["nodeName"] == "IFRAME" and "challenges.cloudflare.com" in attributes.get("src", "")), None)
        if iframe is None:
            return None
        try:
            x, y, *_ = (await self.send("DOM.getBoxModel", session, backendNodeId=iframe["backendNodeId"]))["model"]["content"]
        except RuntimeError:  # Not rendered.
            return None
        # The checkbox is in Turnstile's frame: inlined in `root` if it shares the page's process, else in its own session.
        if frame := self.frames[session].get(iframe.get("frameId")):
            document, offset = (await self.send("DOM.getDocument", frame, depth=-1, pierce=True))["root"], (x, y)
        else:
            frame, document, offset = session, iframe.get("contentDocument"), (0, 0)
        checkbox = next((node for node, attributes in walk(document)
                         if node["nodeName"] == "INPUT" and attributes.get("type") == "checkbox"), None)
        if checkbox is None:
            return None
        with contextlib.suppress(RuntimeError):  # Not rendered yet.
            quad = (await self.send("DOM.getBoxModel", frame, backendNodeId=checkbox["backendNodeId"]))["model"]["content"]
            if quad[2] > quad[0]:
                return offset[0] + (quad[0] + quad[4]) / 2, offset[1] + (quad[1] + quad[5]) / 2
        return None

    async def click(self, session: str, x: float, y: float):
        x, y = x + random.uniform(-2, 2), y + random.uniform(-2, 2)
        start_x, start_y = x + random.uniform(-200, 200), y + random.uniform(80, 200)
        for step in range(1, 4):
            t = step / 3
            await self.send("Input.dispatchMouseEvent", session, type="mouseMoved", x=start_x + (x - start_x) * t, y=start_y + (y - start_y) * t)
        for event in ("mousePressed", "mouseReleased"):
            await self.send("Input.dispatchMouseEvent", session, type=event, x=x, y=y, button="left", clickCount=1)
            await asyncio.sleep(random.uniform(0.03, 0.08))

    async def evaluate(self, session: str, expression: str):
        result = await self.send("Runtime.evaluate", session, expression=expression, returnByValue=True, timeout=None)
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"]["text"])
        return result["result"].get("value")

    async def send(self, method: str, session: str | None = None, timeout: float | None = 30, **params) -> dict:
        id = next(self.ids)
        self.pending[id] = future = asyncio.get_running_loop().create_future()
        message = {"id": id, "method": method, "params": params} | ({"sessionId": session} if session else {})
        self.pipe.write(json.dumps(message).encode() + b"\0")
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(id, None)

    async def start(self):
        to_chrome, from_chrome = os.pipe(), os.pipe()

        def attach_pipes():  # Chrome reads commands from fd 3 and writes replies to fd 4.
            reader, writer = (fcntl.fcntl(fd, fcntl.F_DUPFD, 10) for fd in (to_chrome[0], from_chrome[1]))
            os.dup2(reader, 3)
            os.dup2(writer, 4)

        chrome = find_chrome()
        argv = [chrome, f"--user-data-dir={self.profile}", f"--user-agent={user_agent(chrome)}", *CHROME_FLAGS]
        self.process = await asyncio.create_subprocess_exec(
            *confine(argv, self.profile), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, preexec_fn=attach_pipes, close_fds=False, start_new_session=True)
        os.close(to_chrome[0])
        os.close(from_chrome[1])

        loop = asyncio.get_running_loop()
        self.pipe, _ = await loop.connect_write_pipe(asyncio.Protocol, os.fdopen(to_chrome[1], "wb", 0))
        replies = asyncio.StreamReader(limit=1 << 28)
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(replies), os.fdopen(from_chrome[0], "rb", 0))
        self.reader = asyncio.create_task(self.read(replies))

    async def read(self, replies: asyncio.StreamReader):
        try:
            while True:
                message = json.loads((await replies.readuntil(b"\0"))[:-1])
                if "method" in message:
                    self.dispatch(message)
                    continue
                future = self.pending.get(message.get("id"))
                if future and not future.done():
                    if "error" in message:
                        future.set_exception(RuntimeError(message["error"]["message"]))
                    else:
                        future.set_result(message["result"])
        except asyncio.IncompleteReadError:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("Chrome exited"))

    def dispatch(self, event: dict):
        method, params, parent = event["method"], event["params"], event.get("sessionId")
        if load := self.loads.get(parent):
            if method == "Network.requestWillBeSent":
                if params.get("type") == "Document" and params.get("frameId") == load.frame:
                    load.requests = set()  # A new page: what the last one left unfinished no longer counts.
                # Long-lived streams never finish and `blob:` URLs are local.
                if params.get("type") not in ("EventSource", "Ping", "WebSocket") and not params["request"]["url"].startswith(("blob:", "data:")):
                    load.requests.add(params["requestId"])
            elif method in ("Network.loadingFinished", "Network.loadingFailed"):
                load.requests.discard(params["requestId"])
            elif method == "Network.responseReceived" and params["type"] == "Document" and params.get("frameId") == load.frame:
                load.status = params["response"]["status"]
        if method == "Target.attachedToTarget" and parent and params["targetInfo"]["type"] == "iframe":
            self.frames[parent][params["targetInfo"]["targetId"]] = params["sessionId"]
        elif method == "Target.detachedFromTarget" and parent in self.frames:
            self.frames[parent] = {frame: child for frame, child in self.frames[parent].items() if child != params["sessionId"]}

    def close(self):
        if self.process and self.process.returncode is None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        shutil.rmtree(self.profile, ignore_errors=True)
