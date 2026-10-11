"""Pass Cloudflare challenges in a real, sandboxed, headful Chrome driven over the DevTools pipe.

The window opens in the background, parked off the screen corner, so it never takes focus. It cannot be
minimized or hidden: Chrome throttles rendering there and Turnstile never completes.
"""

import asyncio
import atexit
import contextlib
import fcntl
import itertools
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
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
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
]

OFFSCREEN = 100_000  # The window manager clamps this to the bottom-right corner of the screen.


def detect(headers, body: str) -> bool:
    mitigated = headers.get("cf-mitigated")
    return (mitigated is not None and bytes(mitigated) == b"challenge") or "window._cf_chl_opt" in body


def find_chrome() -> str:
    for candidate in [os.environ.get("BROWSE_ANYTHING_CHROME"), *CHROME_CANDIDATES]:
        if candidate and (path := shutil.which(candidate) or (os.path.isfile(candidate) and candidate)):
            return path
    raise RuntimeError("Chrome not found; set BROWSE_ANYTHING_CHROME")


class Chrome:
    def __init__(self):
        self.profile = tempfile.mkdtemp(prefix="browse-anything-")
        self.process: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.ids = itertools.count(1)
        self.pending: dict[int, asyncio.Future] = {}
        atexit.register(self.close)

    async def solve(self, url: str, timeout: float = 45) -> tuple[str, list[dict], str]:
        """Load `url` until the challenge clears; return the rendered HTML, the site's cookies and Chrome's user agent."""
        async with self.lock:
            if self.process is None or self.process.returncode is not None:
                await self.start()
            target = await self.send("Target.createTarget", url="about:blank", newWindow=True, background=True,
                                     left=OFFSCREEN, top=OFFSCREEN, width=1280, height=900)
            try:
                session = (await self.send("Target.attachToTarget", targetId=target["targetId"], flatten=True))["sessionId"]
                await self.send("Page.navigate", session, url=url)
                html = await asyncio.wait_for(self.pass_challenge(session), timeout)
                host = urlsplit(url).hostname
                cookies = [c for c in (await self.send("Storage.getCookies"))["cookies"]
                           if f".{host}".endswith("." + c["domain"].lstrip("."))]
                return html, cookies, (await self.send("Browser.getVersion"))["userAgent"]
            finally:
                with contextlib.suppress(Exception):
                    await self.send("Target.closeTarget", targetId=target["targetId"])

    async def pass_challenge(self, session: str) -> str:
        last_click = -10.0
        while True:
            await asyncio.sleep(1)
            try:
                html = await self.evaluate(session, "document.readyState == 'complete' && !window._cf_chl_opt && document.documentElement.outerHTML")
            except RuntimeError:  # Mid-navigation.
                continue
            if html:
                return html
            now = asyncio.get_running_loop().time()
            if now - last_click > 5 and (checkbox := await self.find_turnstile(session)):
                await self.click(session, *checkbox)
                last_click = now

    async def find_turnstile(self, session: str) -> tuple[float, float] | None:
        nodes = [(await self.send("DOM.getDocument", session, depth=-1, pierce=True))["root"]]
        while nodes:
            node = nodes.pop()
            attributes = dict(itertools.batched(node.get("attributes", []), 2))
            if node["nodeName"] == "IFRAME" and "challenges.cloudflare.com" in attributes.get("src", ""):
                x0, y0, x1, _, _, y1, *_ = (await self.send("DOM.getBoxModel", session, backendNodeId=node["backendNodeId"]))["model"]["content"]
                return (x0 + 28, (y0 + y1) / 2) if x1 - x0 > 100 else None
            nodes += node.get("children", []) + node.get("shadowRoots", []) + ([node["contentDocument"]] if "contentDocument" in node else [])
        return None

    async def click(self, session: str, x: float, y: float):
        x, y = x + random.uniform(-3, 3), y + random.uniform(-3, 3)
        start_x, start_y = x + random.uniform(-200, 200), y + random.uniform(80, 200)
        for step in range(1, 9):
            t = step / 8
            await self.send("Input.dispatchMouseEvent", session, type="mouseMoved", x=start_x + (x - start_x) * t, y=start_y + (y - start_y) * t)
            await asyncio.sleep(random.uniform(0.02, 0.05))
        for event in ("mousePressed", "mouseReleased"):
            await self.send("Input.dispatchMouseEvent", session, type=event, x=x, y=y, button="left", clickCount=1)
            await asyncio.sleep(random.uniform(0.05, 0.12))

    async def evaluate(self, session: str, expression: str):
        result = await self.send("Runtime.evaluate", session, expression=expression, returnByValue=True)
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"]["text"])
        return result["result"].get("value")

    async def send(self, method: str, session: str | None = None, **params) -> dict:
        id = next(self.ids)
        self.pending[id] = future = asyncio.get_running_loop().create_future()
        message = {"id": id, "method": method, "params": params} | ({"sessionId": session} if session else {})
        self.pipe.write(json.dumps(message).encode() + b"\0")
        try:
            return await asyncio.wait_for(future, 30)
        finally:
            self.pending.pop(id, None)

    async def start(self):
        to_chrome, from_chrome = os.pipe(), os.pipe()

        def attach_pipes():  # Chrome reads commands from fd 3 and writes replies to fd 4.
            reader, writer = (fcntl.fcntl(fd, fcntl.F_DUPFD, 10) for fd in (to_chrome[0], from_chrome[1]))
            os.dup2(reader, 3)
            os.dup2(writer, 4)

        argv = [find_chrome(), f"--user-data-dir={self.profile}", *CHROME_FLAGS]
        if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
            argv = ["xvfb-run", "-a", *argv]  # Headful without a display: a virtual one.
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

    def close(self):
        if self.process and self.process.returncode is None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        shutil.rmtree(self.profile, ignore_errors=True)
