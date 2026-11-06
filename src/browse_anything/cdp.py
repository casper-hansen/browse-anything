"""The Chrome DevTools Protocol over Chrome's debugging pipe (`--remote-debugging-pipe`)."""

import asyncio
import fcntl
import itertools
import json
import os
import signal
import time
import subprocess
from collections.abc import Callable
from typing import Any

Listener = Callable[[str, dict, str | None], Any]  # (method, params, sessionId) -> anything


def exited(pid: int) -> bool:
    """Whether a child has exited, without reaping it, so asyncio's child watcher still gets its status."""
    try:
        with open(f"/proc/{pid}/stat") as stat:
            return stat.read().rsplit(")", 1)[1].split()[0] in ("Z", "X")
    except FileNotFoundError:
        return True
    except OSError:  # No /proc (macOS): reaping is the only way to tell.
        try:
            return os.waitpid(pid, os.WNOHANG)[0] != 0
        except ChildProcessError:
            return True


class ProtocolError(RuntimeError):
    """Chrome answered a command with an error."""


class CDP:
    def __init__(self):
        self.process: asyncio.subprocess.Process | None = None
        self.ids = itertools.count(1)
        self.pending: dict[int, asyncio.Future] = {}
        self.listeners: list[Listener] = []
        self.closed = asyncio.Event()

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None and not self.closed.is_set()

    async def start(self, argv: list[str]):
        """Run `argv`, a Chrome command line with `--remote-debugging-pipe`, and connect to its pipe."""
        to_chrome, from_chrome = os.pipe(), os.pipe()

        def attach_pipes():  # Chrome reads commands from fd 3 and writes replies to fd 4.
            reader, writer = (fcntl.fcntl(fd, fcntl.F_DUPFD, 10) for fd in (to_chrome[0], from_chrome[1]))
            os.dup2(reader, 3)
            os.dup2(writer, 4)

        self.process = await asyncio.create_subprocess_exec(
            *argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=attach_pipes, close_fds=False, start_new_session=True)
        os.close(to_chrome[0])
        os.close(from_chrome[1])

        loop = asyncio.get_running_loop()
        self.pipe, _ = await loop.connect_write_pipe(asyncio.Protocol, os.fdopen(to_chrome[1], "wb", 0))
        replies = asyncio.StreamReader(limit=1 << 30)
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(replies), os.fdopen(from_chrome[0], "rb", 0))
        self.reader = asyncio.create_task(self.read(replies))

    def post(self, method: str, session: str | None = None, extra: dict | None = None, **params) -> asyncio.Future:
        """Write a command now and return the future of its result."""
        if not self.alive:
            raise ConnectionError("Chrome is not running")
        id = next(self.ids)
        self.pending[id] = future = asyncio.get_running_loop().create_future()
        future.add_done_callback(lambda _: self.pending.pop(id, None))
        message = {"id": id, "method": method, "params": params | (extra or {})} | ({"sessionId": session} if session else {})
        self.pipe.write(json.dumps(message).encode() + b"\0")
        return future

    async def send(self, method: str, session: str | None = None, timeout: float | None = 30, extra: dict | None = None, **params) -> dict:
        """Run a command and return its result. `timeout` bounds the wait here; it is not sent to Chrome, but `extra`
        parameters are, for commands that take a parameter named like ours."""
        future = self.post(method, session, extra, **params)
        try:
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            raise TimeoutError(f"{method} did not answer in {timeout:.0f}s") from None

    def on(self, listener: Listener) -> Callable[[], None]:
        """Call `listener` with every event; returns a function that removes it."""
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener) if listener in self.listeners else None

    async def wait_for(self, predicate: Listener, timeout: float | None):
        """The first truthy `predicate(method, params, session)` over the coming events."""
        future = asyncio.get_running_loop().create_future()

        def listener(method, params, session):
            if not future.done() and (value := predicate(method, params, session)):
                future.set_result(value)

        remove = self.on(listener)
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            remove()

    async def read(self, replies: asyncio.StreamReader):
        try:
            while True:
                message = json.loads((await replies.readuntil(b"\0"))[:-1])
                if "method" in message:
                    for listener in list(self.listeners):
                        try:
                            listener(message["method"], message.get("params", {}), message.get("sessionId"))
                        except Exception:  # A broken listener must not stop the connection.
                            import traceback
                            traceback.print_exc()
                    continue
                future = self.pending.get(message.get("id"))
                if future and not future.done():
                    if "error" in message:
                        future.set_exception(ProtocolError(message["error"].get("message", "error")))
                    else:
                        future.set_result(message.get("result", {}))
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.closed.set()
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("Chrome exited"))

    def kill(self, grace: float = 0):
        """Stop Chrome. With `grace`, first ask it to close, so it saves its profile (cookies are written lazily)."""
        if grace and self.alive:
            try:  # The pipe transport writes at once while nothing is queued, so this needs no running loop.
                self.pipe.write(json.dumps({"id": next(self.ids), "method": "Browser.close", "params": {}}).encode() + b"\0")
            except (OSError, RuntimeError):
                pass
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline and not exited(self.process.pid):
                time.sleep(0.05)
        if self.process and self.process.returncode is None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.closed.set()
