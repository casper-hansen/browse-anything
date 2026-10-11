"""Pass Cloudflare challenges in a real, sandboxed Chrome driven over the DevTools pipe.

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
from collections.abc import Awaitable, Callable
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


class Chrome:
    def __init__(self):
        self.profile = tempfile.mkdtemp(prefix="browse-anything-")
        self.process: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.ids = itertools.count(1)
        self.pending: dict[int, asyncio.Future] = {}
        self.frames: defaultdict[str, dict[str, str]] = defaultdict(dict)  # Session -> its out-of-process iframes' frame ids -> sessions.
        self.paused: dict[str, Callable[[dict], Awaitable]] = {}  # Session -> handler of its intercepted requests.
        self.tasks: set[asyncio.Task] = set()
        atexit.register(self.close)

    async def solve(self, url: str, until: str = "clearance", timeout: float = 45) -> tuple[str | None, list[dict], str]:
        """Load `url` in Chrome and pass its challenge; return the rendered HTML, the site's cookies and Chrome's user agent.

        With `until="clearance"` this returns as soon as Cloudflare hands out a new `cf_clearance` cookie, and the
        HTML is None: the request that would load the page with it is cancelled, so the caller can fetch the page
        itself without the origin serving it twice. With `until="page"` it waits for the rendered page.
        """
        async with self.lock:
            if self.process is None or self.process.returncode is not None:
                await self.start()
            host = urlsplit(url).hostname
            before = clearance(await self.cookies(host))
            target = await self.send("Target.createTarget", url="about:blank", newWindow=True, width=1280, height=900)
            session = None
            try:
                session = (await self.send("Target.attachToTarget", targetId=target["targetId"], flatten=True))["sessionId"]
                # Out-of-process iframes, such as Turnstile's, get their own sessions so their DOM can be inspected.
                await self.send("Target.setAutoAttach", session, autoAttach=True, waitForDebuggerOnStart=False, flatten=True)
                cleared = asyncio.get_running_loop().create_future()
                if until == "clearance":
                    self.paused[session] = lambda event: self.hold_document(session, event, host, before, cleared)
                    await self.send("Fetch.enable", session, patterns=[{"resourceType": "Document", "requestStage": "Request"}])
                await self.send("Page.navigate", session, url=url)
                challenge = asyncio.ensure_future(self.pass_challenge(session, host))
                try:
                    done, _ = await asyncio.wait([challenge, cleared], timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    challenge.cancel()
                if not done:
                    raise TimeoutError(f"Cloudflare challenge on {url} not passed in {timeout:.0f}s")
                html, cookies = done.pop().result()
                return html, cookies, (await self.send("Browser.getVersion"))["userAgent"]
            finally:
                self.paused.pop(session, None)
                self.frames.pop(session, None)
                with contextlib.suppress(Exception):
                    await self.send("Target.closeTarget", targetId=target["targetId"])

    async def hold_document(self, session: str, event: dict, host: str, before: str | None, cleared: asyncio.Future):
        """Let a document request through, unless it is the one that would load the page with a new clearance."""
        request = event["requestId"]
        with contextlib.suppress(Exception):  # The target may already be closing.
            cookies = await self.cookies(host)
            if clearance(cookies) not in (None, before) and not cleared.done():
                cleared.set_result((None, cookies))
                await self.send("Fetch.failRequest", session, requestId=request, errorReason="Aborted")
            else:
                await self.send("Fetch.continueRequest", session, requestId=request)

    async def pass_challenge(self, session: str, host: str) -> tuple[str, list[dict]]:
        clicked = -RECLICK
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(POLL)
            try:
                html = await self.evaluate(session, "document.readyState == 'complete' && !window._cf_chl_opt && document.documentElement.outerHTML")
            except (RuntimeError, asyncio.TimeoutError):  # Mid-navigation.
                continue
            if html:
                return html, await self.cookies(host)
            if loop.time() - clicked > RECLICK and (checkbox := await self.find_turnstile(session)):
                await self.click(session, *checkbox)
                clicked = loop.time()

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
        params, parent = event["params"], event.get("sessionId")
        if event["method"] == "Fetch.requestPaused" and (handler := self.paused.get(parent)):
            task = asyncio.create_task(handler(params))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        elif event["method"] == "Target.attachedToTarget" and parent and params["targetInfo"]["type"] == "iframe":
            self.frames[parent][params["targetInfo"]["targetId"]] = params["sessionId"]
        elif event["method"] == "Target.detachedFromTarget" and parent in self.frames:
            self.frames[parent] = {frame: child for frame, child in self.frames[parent].items() if child != params["sessionId"]}

    def close(self):
        if self.process and self.process.returncode is None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        shutil.rmtree(self.profile, ignore_errors=True)


def clearance(cookies: list[dict]) -> str | None:
    return next((c["value"] for c in cookies if c["name"] == "cf_clearance"), None)
