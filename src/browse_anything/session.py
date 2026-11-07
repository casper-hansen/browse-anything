"""A long-lived, sandboxed Chrome whose tabs stay open between tool calls.

Every page target is attached at browser level and paused until it is set up, so popups are tracked from their first
request. Nothing here calls `Runtime.enable` on public sites, since pages can detect it: page errors are reported
from an isolated world through a binding instead, and full console capture is only turned on for local hosts (or
everywhere, if configured).
"""

import asyncio
import base64
import contextlib
import contextvars
import fcntl
import ipaddress
import itertools
import json
import math
import os
import random
import re
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urljoin, urlsplit

from . import cloudflare, keys
from .cdp import CDP, ProtocolError
from .chrome import launch
from .snapshot import REF, Frame, Item, build, controls, find_item, walk_items

# Who a tool call is for. One server serves every session of an MCP client such as DSH, which says which session each
# call comes from; each caller gets its own tabs and current tab, so two agents browsing at once cannot steer each
# other's page. Calls that do not say share the "" caller.
CALLER: contextvars.ContextVar[str] = contextvars.ContextVar("caller", default="")

WORLD = "__browse_anything__"
RELAY = "browse-anything-" + secrets.token_hex(8)  # The DOM event name main-world errors are relayed under.
# Uncaught errors are only reported in the world that threw them, so a listener in the page's own world relays them.
# It touches no page globals: it holds its own references, so nothing the page can enumerate changes. Its DOM event
# has a random name the page cannot know to listen for.
MAIN_SCRIPT = f"""(() => {{
    const add = EventTarget.prototype.addEventListener, dispatch = EventTarget.prototype.dispatchEvent;
    const Custom = CustomEvent, stringify = JSON.stringify, String_ = String, doc = document;
    const relay = (text, location) => {{ try {{ dispatch.call(doc, new Custom('{RELAY}', {{detail: stringify([text, location])}})); }} catch (e) {{}} }};
    add.call(window, 'error', e => {{ if (e.target === window) relay(String_(e.error && e.error.stack || e.message || 'Script error'), e.filename ? e.filename + ':' + e.lineno : ''); }}, true);
    add.call(window, 'unhandledrejection', e => {{ const r = e.reason; relay('Uncaught (in promise) ' + String_(r && (r.stack || r.message) || r), ''); }});
}})()"""
# Runs in the isolated world of every frame, which shares the DOM with the page but none of its JavaScript, and keeps
# the relayed errors until they are collected.
WORLD_SCRIPT = f"""(() => {{
    window.__errors = [];
    document.addEventListener('{RELAY}', e => {{
        try {{ if (window.__errors.length < 500) window.__errors.push(JSON.parse(e.detail)); }} catch (err) {{}}
    }});
}})()"""
COLLECT = "(() => { const errors = window.__errors || []; window.__errors = []; return errors; })()"

# What a failed navigation most likely means, for the errors a model can do something about.
NET_HINTS = {
    "net::ERR_NAME_NOT_RESOLVED": " (the domain does not exist; check its spelling)",
    "net::ERR_CONNECTION_REFUSED": " (nothing is listening there; for a local server, check it is running and the port)",
    "net::ERR_CONNECTION_TIMED_OUT": " (the server did not answer)",
    "net::ERR_CERT_COMMON_NAME_INVALID": " (the site's certificate is not for this domain)",
    "net::ERR_CERT_DATE_INVALID": " (the site's certificate has expired)",
    "net::ERR_INTERNET_DISCONNECTED": " (no internet connection)",
}

# The state of the page's Turnstile widgets: "none", "passed", or "pending". Each widget has a hidden input that gets
# its token once it passes. (Its iframe is no sign: out of process, it is not in the page's frame tree.)
TURNSTILE_STATE = """(() => {
    const fields = [...document.querySelectorAll('input[name="cf-turnstile-response"]')];
    return !fields.length ? 'none' : fields.every(f => f.value) ? 'passed' : 'pending';
})()"""

# Resolves once the DOM has not changed for IDLE ms and no finite animation runs, or after LIMIT ms.
SETTLE_DOM = """new Promise(resolve => {
    const start = performance.now();
    let last = start;
    const observer = new MutationObserver(() => { last = performance.now(); });
    observer.observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
    const animating = () => document.getAnimations().some(a => {
        if (a.playState !== 'running') return false;
        const end = a.effect && a.effect.getComputedTiming ? a.effect.getComputedTiming().endTime : Infinity;
        return Number.isFinite(end);
    });
    const tick = () => {
        const now = performance.now();
        if (animating()) last = now;  // A fade counts as change: the element is often removed when it ends.
        if (now - last >= IDLE || now - start >= LIMIT) { observer.disconnect(); resolve(now - start); }
        else setTimeout(tick, 50);
    };
    setTimeout(tick, 50);
})"""

POLL = 0.25  # Seconds between checks of a challenge's progress.
RECLICK = 4  # Seconds to let a click verify before clicking a checkbox that is still shown.
CHALLENGE_TIMEOUT = 45
LOAD_TIMEOUT = 30
SCROLL_STEP, SCROLL_PAUSE, SCROLL_MAX = 600, 0.15, 20_000  # Pixels and seconds; scrolling loads lazy content.
IDLE = 0.5  # Seconds without network requests after which a page counts as settled after an action.
STATIC_TYPES = {"Image", "Media", "Font", "Stylesheet", "Script", "Manifest", "TextTrack"}
LONG_LIVED = {"EventSource", "Ping", "WebSocket"}
MAX_REQUESTS, MAX_MESSAGES = 5000, 5000
LEVELS = {"debug": 0, "info": 1, "warning": 2, "error": 3}

SERIALIZE = """(() => {
    const roots = [];
    const collect = root => root.querySelectorAll('*').forEach(e => { if (e.shadowRoot) { roots.push(e.shadowRoot); collect(e.shadowRoot); } });
    collect(document);
    const html = document.documentElement;
    return '<!DOCTYPE html>' + (html.getHTML ? html.getHTML({shadowRoots: roots}) : html.outerHTML);
})()"""

QUERY = r"""function(selector) {
    const deep = (root, sel) => {
        const out = [...root.querySelectorAll(sel)];
        for (const el of root.querySelectorAll('*')) if (el.shadowRoot) out.push(...deep(el.shadowRoot, sel));
        return out;
    };
    if (/^(xpath=|\/\/|\(\/\/)/.test(selector)) {
        const r = document.evaluate(selector.replace(/^xpath=/, ''), document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
        return Array.from({length: r.snapshotLength}, (_, i) => r.snapshotItem(i));
    }
    if (selector.startsWith('text=')) {
        let text = selector.slice(5), exact = /^".*"$/.test(text);
        if (exact) text = text.slice(1, -1);
        const norm = s => (s || '').replace(/\s+/g, ' ').trim();
        const hits = deep(document, 'body *').filter(e => {
            const t = norm(e.innerText ?? e.textContent);
            return exact ? t === text : t.toLowerCase().includes(text.toLowerCase());
        });
        return hits.filter(e => !hits.some(o => o !== e && e.contains(o)));
    }
    return deep(document, selector.replace(/^css=/, ''));
}"""

# With `this` the target: null if a click at (x, y) in the viewport reaches it, else a description of what covers it.
COVERS = """function(x, y) {
    let hit = document.elementFromPoint(x, y);
    while (hit && hit.shadowRoot) {
        const inner = hit.shadowRoot.elementFromPoint(x, y);
        if (!inner || inner === hit) break;
        hit = inner;
    }
    if (!hit) return null;
    for (let n = hit; n; n = n.parentNode || n.host) {
        if (n === this) return null;
        if (n.tagName === 'LABEL' && n.control === this) return null;  // Custom checkboxes hide the input under its label.
    }
    let s = hit.tagName.toLowerCase();
    if (hit.id) s += '#' + hit.id;
    if (typeof hit.className === 'string' && hit.className.trim()) s += '.' + hit.className.trim().split(/\\s+/).slice(0, 3).join('.');
    const text = (hit.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 60);
    return '<' + s + '>' + (text ? ' "' + text + '"' : '');
}"""

SELECT_ALL = """function() {
    if (this instanceof HTMLInputElement || this instanceof HTMLTextAreaElement) {
        try { this.select(); } catch (e) {}
        try { this.setSelectionRange(0, this.value.length); } catch (e) {}
        return true;
    }
    if (this.isContentEditable) {
        const range = document.createRange();
        range.selectNodeContents(this);
        const selection = getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        return true;
    }
    return false;
}"""

SELECT_OPTIONS = """function(values) {
    if (this.tagName !== 'SELECT') throw new Error('Element is not a <select> element');
    const options = [...this.options], chosen = [];
    for (const value of values) {
        const option = options.find(o => o.value === value) || options.find(o => o.label === value || o.text.trim() === value);
        if (!option) throw new Error('Option "' + value + '" not found. Options: ' + options.map(o => JSON.stringify(o.label || o.value)).join(', '));
        chosen.push(option);
        if (!this.multiple) break;
    }
    for (const option of options) option.selected = chosen.includes(option);
    this.dispatchEvent(new Event('input', {bubbles: true}));
    this.dispatchEvent(new Event('change', {bubbles: true}));
    return chosen.map(o => o.label || o.value);
}"""

STATE = """function() {
    const e = this;
    return {tag: e.tagName, type: (e.type || '').toLowerCase(), checked: 'checked' in e ? e.checked : e.getAttribute('aria-checked') === 'true'};
}"""

SET_VALUE = """function(value) {
    const proto = Object.getPrototypeOf(this);
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    setter ? setter.call(this, value) : (this.value = value);
    this.dispatchEvent(new Event('input', {bubbles: true}));
    this.dispatchEvent(new Event('change', {bubbles: true}));
}"""


class ToolError(Exception):
    """A failure the model can act on; shown as the tool's error."""


@dataclass
class Config:
    profile: str = ""  # A persistent Chrome profile directory; empty uses a throwaway one.
    output_dir: str = ""
    console: Literal["auto", "stealth", "full"] = "auto"
    width: int = 1280
    height: int = 720
    # Tabs unused for this many minutes are closed. One server serves every agent session, which MCP does not tell
    # apart, so this keeps a new task from starting among another's leftover tabs. 0 keeps tabs open.
    tab_idle_minutes: float = 60


@dataclass
class Element:
    session: str
    backend: int
    frame: str | None = None  # The frame id for elements in same-process child frames.


@dataclass
class Request:
    index: int
    id: str
    session: str
    url: str
    method: str
    type: str
    headers: dict
    has_post: bool = False
    post: str | None = None
    status: int | None = None
    status_text: str = ""
    response_headers: dict = field(default_factory=dict)
    mime: str = ""
    failed: str | None = None
    finished: bool = False

    def line(self) -> str:
        if self.failed:
            result = f"[FAILED] {self.failed}"
        elif self.status is None:
            result = "[pending]"
        else:
            result = f"[{self.status}] {self.status_text}".rstrip()
        return f"{self.index}. [{self.method}] {self.url} => {result}"


@dataclass
class Message:
    level: str  # error | warning | info | debug
    kind: str  # Shown in brackets: LOG, ERROR, WARNING, INFO, DEBUG, ...
    text: str
    location: str = ""
    browser: bool = False  # Reported by Chrome itself (failed loads, preload hints, ...) rather than the page's code.

    def line(self, limit: int | None = None) -> str:
        text = self.text if limit is None or len(self.text) <= limit else self.text[:limit - 1] + "…"
        return f"[{self.kind}] {text}" + (f" @ {self.location}" if self.location else "")


def is_local(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme in ("file", "data", "about", "chrome"):
        return True
    host = (parts.hostname or "").lower()
    if host in ("localhost", "") or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa", ".ts.net")):
        return True
    with contextlib.suppress(ValueError):
        address = ipaddress.ip_address(host)
        return address.is_private or address.is_loopback or address.is_link_local
    return False


def remote_value(obj: dict) -> str:
    """A RemoteObject as console text: strings raw, everything else like DevTools previews it."""
    if obj.get("type") == "string":
        return obj.get("value", "")
    if "value" in obj:
        return json.dumps(obj["value"]) if not isinstance(obj["value"], str) else obj["value"]
    if obj.get("unserializableValue"):
        return obj["unserializableValue"]
    preview = obj.get("preview")
    if preview and preview.get("type") == "object" and preview.get("subtype") not in ("node", "error", "regexp", "date"):
        inner = ", ".join((f"{p['name']}: " if preview.get("subtype") != "array" else "") + p.get("value", p.get("type", ""))
                          for p in preview.get("properties", []))
        if preview.get("overflow"):
            inner += ", …"
        return f"[{inner}]" if preview.get("subtype") == "array" else f"{{{inner}}}"
    return obj.get("description") or obj.get("type", "")


class Tab:
    def __init__(self, browser: "Session", target: str, session: str, context: str | None, opener: str | None, hidden: bool):
        self.browser, self.cdp = browser, browser.cdp
        self.target, self.id, self.frame = target, session, target  # A tab's main frame has its target's id.
        self.context, self.opener, self.hidden = context, opener, hidden
        self.caller = ""  # The caller whose tab this is (see CALLER).
        self.lock = asyncio.Lock()
        self.ready = asyncio.Event()
        self.closed = False
        self.used = time.monotonic()  # When a tool last acted on this tab.
        self.last_quads: list[list[float]] = []  # The viewport quads of the element box() measured last.
        self.url = "about:blank"
        self.status: int | None = None
        self.statuses: dict[str, int] = {}  # Navigation loaderId -> its document's HTTP status.
        self.loading = False
        self.navigations = 0
        self.inflight: set[str] = set()
        self.requests: list[Request] = []
        self.by_request: dict[tuple[str, str], Request] = {}
        self.request_index = itertools.count(1)
        self.network_start = 0  # Index into `requests` of the current page's first request.
        self.loaders: dict[str, int] = {}  # Navigation loaderId -> index of its document request.
        self.messages: list[Message] = []
        self.console_start = 0
        self.console_reported = 0
        self.dialog: dict | None = None
        self.chooser: dict | None = None
        self.modal = asyncio.Event()
        self.events: list[str] = []
        self.frames: dict[str, str] = {}  # Out-of-process iframes: frame id -> session.
        self.parents: dict[str, tuple[str, str]] = {}  # Iframe session -> (parent session, frame id).
        self.refs: dict[str, Element] = {}
        self.ref_ids: dict[tuple[str, int], str] = {}
        self.ref_counters: dict[str, itertools.count] = {}
        self.prefixes: dict[tuple[str, str | None], str] = {}  # (session, frame id) -> ref prefix.
        self.prefix_counter = itertools.count(1)
        self.items: list[Item | str] = []
        self.mouse = (random.uniform(200, 600), random.uniform(150, 400))
        self.media: dict = {}
        self.runtime = False
        self.downloads: set[str] = set()

    # Setup and events.

    def setup(self) -> list[asyncio.Future]:
        """Write the commands that set the tab up. A new page waiting for the debugger answers them only once it runs,
        so they are sent first and awaited after it is resumed."""
        post = lambda method, **params: self.cdp.post(method, self.id, **params)
        sent = [
            post("Page.enable"),
            post("Network.enable"),
            post("Log.enable"),
            post("Target.setAutoAttach", autoAttach=True, waitForDebuggerOnStart=False, flatten=True),
            post("Page.addScriptToEvaluateOnNewDocument", source=WORLD_SCRIPT, worldName=WORLD, runImmediately=True),
            post("Page.addScriptToEvaluateOnNewDocument", source=MAIN_SCRIPT, runImmediately=True),
        ]
        if not self.hidden:
            sent.append(post("Page.setInterceptFileChooserDialog", enabled=True))
        if hints := getattr(self.cdp, "client_hints", None):
            sent.append(post("Emulation.setUserAgentOverride", userAgent=self.cdp.user_agent, userAgentMetadata=hints))
        if self.browser.config.console == "full":
            self.runtime = True
            sent.append(post("Runtime.enable"))
        return sent

    async def setup_frame(self, session: str):
        post = lambda method, **params: self.cdp.post(method, session, **params)
        sent = [post("Page.enable"), post("Network.enable"),
                post("Target.setAutoAttach", autoAttach=True, waitForDebuggerOnStart=False, flatten=True)]
        if hints := getattr(self.cdp, "client_hints", None):  # Out-of-process iframes send their own hints.
            sent.append(post("Emulation.setUserAgentOverride", userAgent=self.cdp.user_agent, userAgentMetadata=hints))
        if not self.hidden:
            sent.append(post("Page.setInterceptFileChooserDialog", enabled=True))
        await asyncio.wait_for(asyncio.gather(*sent, return_exceptions=True), 30)

    async def set_runtime(self, enabled: bool):
        if enabled != self.runtime:
            self.runtime = enabled
            with contextlib.suppress(ProtocolError, ConnectionError):
                await self.send("Runtime.enable" if enabled else "Runtime.disable")

    def want_runtime(self, url: str) -> bool:
        mode = self.browser.config.console
        return mode == "full" or (mode == "auto" and not self.hidden and is_local(url))

    def handle(self, method: str, params: dict, session: str):
        if method == "Network.requestWillBeSent":
            request = params["request"]
            if params.get("type") == "Document" and params.get("frameId") == self.frame and session == self.id:
                if "redirectResponse" not in params:
                    self.navigations += 1
                    self.network_start = len(self.requests)
                    self.loaders[params.get("loaderId")] = len(self.requests)
                self.loading = True
                self.inflight = set()
                if self.want_runtime(request["url"]) != self.runtime:
                    asyncio.ensure_future(self.set_runtime(not self.runtime))
            key = (session, params["requestId"])
            if previous := self.by_request.get(key):  # A redirect reuses the request id.
                response = params.get("redirectResponse", {})
                previous.status, previous.status_text = response.get("status"), response.get("statusText", "")
                previous.response_headers, previous.finished = response.get("headers", {}), True
            entry = Request(next(self.request_index), params["requestId"], session, request["url"], request["method"],
                            params.get("type", "Other"), request.get("headers", {}), request.get("hasPostData", False),
                            request.get("postData"))
            self.requests.append(entry)
            self.by_request[key] = entry
            if len(self.requests) > MAX_REQUESTS:
                dropped = self.requests.pop(0)
                self.by_request.pop((dropped.session, dropped.id), None)
                self.network_start = max(0, self.network_start - 1)
                self.loaders = {k: v - 1 for k, v in self.loaders.items() if v > 0}
            if params.get("type") not in LONG_LIVED and not request["url"].startswith(("blob:", "data:")):
                self.inflight.add(params["requestId"])
        elif method == "Network.responseReceived":
            response = params["response"]
            if entry := self.by_request.get((session, params["requestId"])):
                entry.status, entry.status_text = response.get("status"), response.get("statusText", "")
                entry.response_headers, entry.mime = response.get("headers", {}), response.get("mimeType", "")
            if params.get("type") == "Document" and params.get("frameId") == self.frame and session == self.id:
                self.status = response.get("status")
                self.statuses[params.get("loaderId")] = self.status
        elif method in ("Network.loadingFinished", "Network.loadingFailed"):
            self.inflight.discard(params["requestId"])
            if entry := self.by_request.get((session, params["requestId"])):
                entry.finished = True
                if method == "Network.loadingFailed":
                    entry.failed = params.get("errorText", "failed") + (" (blocked)" if params.get("blockedReason") else "")
        elif method == "Page.frameNavigated" and session == self.id and not params["frame"].get("parentId"):
            self.url = params["frame"]["url"]
            # The status of this page's own load: a page restored from the back/forward cache made no request, and
            # must not show the status of the page before it.
            self.status = self.statuses.get(params["frame"].get("loaderId"))
            # A page restored from the back/forward cache made no request; its list starts empty.
            self.network_start = len(self.requests) if params.get("type") == "BackForwardCacheRestore" else \
                self.loaders.get(params["frame"].get("loaderId"), len(self.requests))
            self.console_start = self.console_reported = len(self.messages)
            self.refs, self.ref_ids, self.ref_counters, self.prefixes, self.items = {}, {}, {}, {}, []
        elif method == "Page.navigatedWithinDocument" and session == self.id and params.get("frameId") == self.frame:
            self.url = params["url"]
        elif method == "Page.frameStartedLoading" and params.get("frameId") == self.frame:
            self.loading = True
        elif method in ("Page.frameStoppedLoading", "Page.loadEventFired") and (params.get("frameId", self.frame) == self.frame) and session == self.id:
            self.loading = False
        elif method == "Page.javascriptDialogOpening":
            if params.get("type") == "beforeunload" or self.hidden:  # Leaving a page is what was asked for.
                asyncio.ensure_future(self.send("Page.handleJavaScriptDialog", session, accept=True))
                return
            self.dialog = params | {"session": session}
            self.modal.set()
        elif method == "Page.javascriptDialogClosed":
            self.dialog = None
            if not self.chooser:
                self.modal.clear()
        elif method == "Page.fileChooserOpened":
            self.chooser = params | {"session": session}
            self.modal.set()
        elif method == "Runtime.consoleAPICalled":
            kind = params["type"]
            level = {"error": "error", "assert": "error", "warning": "warning", "debug": "debug"}.get(kind, "info")
            frame = (params.get("stackTrace") or {}).get("callFrames") or [{}]
            location = f"{frame[0].get('url', '')}:{frame[0].get('lineNumber', 0)}" if frame[0] else ""
            self.log(Message(level, kind.upper(), " ".join(remote_value(arg) for arg in params.get("args", [])), location))
        elif method == "Runtime.exceptionThrown":
            details = params["exceptionDetails"]
            text = (details.get("exception") or {}).get("description") or details.get("text", "Uncaught error")
            self.log(Message("error", "ERROR", text, f"{details.get('url', '')}:{details.get('lineNumber', 0)}"))
        elif method == "Log.entryAdded":
            entry = params["entry"]
            level = {"verbose": "debug", "info": "info", "warning": "warning", "error": "error"}.get(entry["level"], "info")
            location = f"{entry['url']}:{entry.get('lineNumber', 0)}" if entry.get("url") else ""
            self.log(Message(level, level.upper(), entry["text"], location, browser=True))

    def log(self, message: Message):
        self.messages.append(message)
        if len(self.messages) > MAX_MESSAGES:
            self.messages.pop(0)
            self.console_start = max(0, self.console_start - 1)
            self.console_reported = max(0, self.console_reported - 1)

    async def send(self, method: str, session: str | None = None, timeout: float | None = 30, extra: dict | None = None, **params) -> dict:
        return await self.cdp.send(method, session or self.id, timeout, extra, **params)

    # JavaScript.

    async def world(self, frame: str | None = None, session: str | None = None) -> int:
        """The isolated world's execution context in `frame` (default: the tab's main frame)."""
        if frame is None and session and session != self.id:
            frame = self.parents[session][1]
        result = await self.send("Page.createIsolatedWorld", session, frameId=frame or self.frame, worldName=WORLD)
        return result["executionContextId"]

    async def eval(self, expression: str, isolated: bool = False, timeout: float = 10, session: str | None = None):
        params = {"expression": expression, "returnByValue": True, "awaitPromise": True}
        if isolated:
            params["contextId"] = await self.world(session=session)
        result = await self.send("Runtime.evaluate", session, timeout=timeout, **params)
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            raise ProtocolError((details.get("exception") or {}).get("description") or details.get("text", "error"))
        return result["result"].get("value")

    async def call(self, element: Element, function: str, *args, isolated: bool = True, by_value: bool = True, timeout: float = 10):
        """Call `function` with `this` bound to `element`."""
        params = {"backendNodeId": element.backend}
        if isolated:
            params["executionContextId"] = await self.world(element.frame, element.session)
        try:
            object_id = (await self.send("DOM.resolveNode", element.session, **params))["object"]["objectId"]
        except ProtocolError:
            raise ToolError("The element is no longer in the page. Take a new snapshot to get fresh refs.") from None
        result = await self.send("Runtime.callFunctionOn", element.session, timeout=timeout, functionDeclaration=function,
                                 objectId=object_id, arguments=[a if isinstance(a, dict) and "objectId" in a else {"value": a} for a in args],
                                 returnByValue=by_value, awaitPromise=True, userGesture=True)
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            raise ToolError((details.get("exception") or {}).get("description") or details.get("text", "error"))
        return result["result"].get("value") if by_value else result["result"]

    # Loading.

    async def wait_loaded(self, timeout: float = LOAD_TIMEOUT) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.dialog:
                return False
            if not self.loading:
                with contextlib.suppress(ProtocolError, TimeoutError, ConnectionError):
                    if await self.eval("document.readyState", isolated=True, timeout=5) == "complete":
                        return True
            await asyncio.sleep(0.1)
        return False

    async def quiet(self, limit: float, idle: float = IDLE):
        """Wait until no requests have been in flight for `idle` seconds, for at most `limit` seconds."""
        deadline = time.monotonic() + limit
        calm = time.monotonic()
        while time.monotonic() < deadline and time.monotonic() - calm < idle and not self.dialog:
            await asyncio.sleep(0.05)
            if self.inflight:
                calm = time.monotonic()

    async def pass_challenges(self, timeout: float = CHALLENGE_TIMEOUT):
        """Wait out bot-wall interstitials, clicking Turnstile's checkbox when it shows."""
        deadline = time.monotonic() + timeout
        clicked, kind_seen = -RECLICK, None
        while not self.dialog:
            try:
                kind = await self.eval(cloudflare.INTERSTITIAL, timeout=5)
            except (ProtocolError, TimeoutError):  # Mid-navigation.
                kind = "navigating"
            if kind is None:
                if kind_seen:  # A passed challenge reloads or redirects; let the real page finish.
                    await self.wait_loaded(10)
                return
            if kind not in ("blank", "loading", "navigating"):
                kind_seen = kind
            if time.monotonic() > deadline:
                if kind_seen:
                    raise ToolError(f"{self.url} did not get past the {kind_seen} challenge in {timeout:.0f}s")
                return
            if time.monotonic() - clicked > RECLICK and kind_seen and (checkbox := await self.turnstile()):
                await self.click_at(*checkbox)
                clicked = time.monotonic()
            await asyncio.sleep(POLL)

    async def turnstile(self) -> tuple[float, float] | None:
        with contextlib.suppress(ProtocolError, TimeoutError):
            return await cloudflare.find_turnstile(self.cdp, self.id, self.frames)
        return None

    async def embedded_turnstile(self, settle: float = 4, limit: float = 25):
        """See a Turnstile widget embedded in a regular page through, clicking its checkbox if it asks for one.

        Such widgets guard login forms, and whole pages too: Bing's "One last step" page is one. A widget starts out
        "Verifying..." and either passes by itself or shows a checkbox a few seconds later, so it is watched until it
        has put its token in the page, for `settle` seconds if it shows no checkbox, and at most `limit` seconds.
        """
        started, clicked, navigations = time.monotonic(), -RECLICK, self.navigations
        while not self.dialog and self.navigations == navigations:
            try:
                if await self.eval(TURNSTILE_STATE, isolated=True, timeout=5) != "pending":
                    return
                checkbox = await self.turnstile()
            except (ProtocolError, TimeoutError, ConnectionError):
                return
            elapsed = time.monotonic() - started
            if elapsed > limit or (checkbox is None and clicked < 0 and elapsed > settle):
                return
            if checkbox and time.monotonic() - clicked > RECLICK:
                await self.click_at(*checkbox)
                clicked = time.monotonic()
            await asyncio.sleep(0.5)
        # Passing it sent the page on, as Bing's does: let the next page load, past any challenge of its own.
        if self.navigations != navigations and not self.dialog:
            await self.wait_loaded()
            await self.pass_challenges()

    async def navigate(self, url: str):
        url = url.strip()
        if url.startswith(("/", "?", "#")) and self.url.startswith("http"):  # A path, as snapshots show same-site links.
            url = urljoin(self.url, url)
        elif not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", url):
            url = ("http://" if is_local("http://" + url) else "https://") + url
        await self.set_runtime(self.want_runtime(url))
        previous, before = self.url, await self.history_index()
        self.loading = True
        downloads = len(self.downloads)
        result = await self.send("Page.navigate", timeout=LOAD_TIMEOUT * 2, url=url)
        if error := result.get("errorText"):
            await asyncio.sleep(0.3)
            if error == "net::ERR_ABORTED" and len(self.downloads) > downloads:
                await self.browser.finish_downloads(self)
                return
            self.loading = False
            raise ToolError(f"{error} at {url}{NET_HINTS.get(error, '')}{await self.restore(before, previous)}")
        await self.settle_navigation(lazy=False)

    async def history_index(self) -> int | None:
        with contextlib.suppress(ProtocolError, TimeoutError):
            return (await self.send("Page.getNavigationHistory", timeout=5))["currentIndex"]
        return None

    async def restore(self, before: int | None, previous: str) -> str:
        """After a failed navigation, go back from Chrome's error page so the tab keeps the page it had."""
        if before is None or previous in ("", "about:blank"):
            return ""
        with contextlib.suppress(ProtocolError, TimeoutError, KeyError, IndexError):
            history = await self.send("Page.getNavigationHistory", timeout=5)
            if history["currentIndex"] > before:
                await self.send("Page.navigateToHistoryEntry", entryId=history["entries"][before]["id"])
                await self.wait_loaded(10)
            return f". The tab still shows {previous}"
        return ""

    async def settle_navigation(self, lazy: bool, limit: float = 3):
        await self.wait_loaded()
        await self.pass_challenges()
        await self.embedded_turnstile()
        if lazy:
            await self.scroll_through()
        await self.quiet(limit)

    async def scroll_through(self, limit: float = 10):
        """Scroll to the bottom, so lazy content loads, and back to the top."""
        deadline = time.monotonic() + limit
        with contextlib.suppress(ProtocolError, TimeoutError):
            y = 0
            while y < min(SCROLL_MAX, await self.eval("document.documentElement.scrollHeight", isolated=True)) and time.monotonic() < deadline:
                y += SCROLL_STEP
                await self.eval(f"window.scrollTo(0, {y})", isolated=True)
                await asyncio.sleep(SCROLL_PAUSE)
        await self.quiet(max(0.0, deadline - time.monotonic()), idle=1)

    async def after_action(self, navigations: int, downloads: int, limit: float = 2):
        """Let what an action started finish: navigations (and their challenges), requests, and downloads."""
        await asyncio.sleep(0.15)
        if self.dialog or self.chooser:
            return
        if self.navigations != navigations:
            await self.settle_navigation(lazy=False)
        else:
            # The network going quiet and the page's DOM settling are waited for together.
            await asyncio.gather(self.quiet(limit), self.settle_dom())
        if len(self.downloads) > downloads:
            await self.browser.finish_downloads(self)

    async def settle_dom(self, idle: float = 0.4, limit: float = 1.5):
        """Wait until the page stops changing: no DOM mutations for `idle` seconds and no finite CSS animation or
        transition running, for at most `limit` seconds. A cookie banner that fades out after its "Reject all" is
        clicked is otherwise still in the snapshot, and the click looks like it failed."""
        with contextlib.suppress(ProtocolError, TimeoutError, ConnectionError):
            await self.eval(SETTLE_DOM.replace("IDLE", str(int(idle * 1000))).replace("LIMIT", str(int(limit * 1000))),
                            isolated=True, timeout=limit + 2)

    async def act(self, coroutine):
        """Run an action; return early (leaving it running) if it opens a dialog or file chooser."""
        task = asyncio.ensure_future(coroutine)
        modal = asyncio.ensure_future(self.modal.wait())
        done, _ = await asyncio.wait({task, modal}, return_when=asyncio.FIRST_COMPLETED)
        modal.cancel()
        if task in done:
            return task.result()
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        return None

    # Targets.

    def ref_for(self, prefix: str, backend: int) -> str:
        """An element's ref. One numbering spans all frames, and refs carry no frame prefix: with per-frame
        numbering, `f4e27` (a consent banner's button) and `e27` (the page's search box) both existed, and a model
        that dropped the prefix clicked the wrong one. The ref's frame is recorded in `refs` instead."""
        key = (prefix, backend)
        if key not in self.ref_ids:
            counter = self.ref_counters.setdefault("", itertools.count(1))
            self.ref_ids[key] = f"e{next(counter)}"
        return self.ref_ids[key]

    async def resolve(self, target: str) -> Element:
        target = target.strip()
        if REF.match(target):
            if target not in self.refs:
                raise ToolError(f"Ref {target} not found in the current page snapshot. Try capturing a new snapshot.")
            return self.refs[target]
        if match := re.fullmatch(r"""role=(\w+)(?:\[name=(["'])(.*)\2(i?)\])?""", target):
            role, name, insensitive = match[1], match[3], match[4]
            found = [item for item in walk_items(self.items) if item.role == role and item.ref and
                     (name is None or (item.name.lower() == name.lower() if insensitive else item.name == name))]
            if len(found) != 1:
                raise ToolError(f"Selector {target!r} matched {len(found)} elements in the last snapshot; it must match one")
            return self.refs[found[0].ref]
        context = await self.world()
        try:
            array = (await self.send("Runtime.callFunctionOn", functionDeclaration=QUERY, executionContextId=context,
                                     arguments=[{"value": target}], returnByValue=False))
        except ProtocolError as error:
            raise ToolError(f"Invalid selector {target!r}: {error}") from None
        if "exceptionDetails" in array:
            details = array["exceptionDetails"]
            raise ToolError(f"Invalid selector {target!r}: {(details.get('exception') or {}).get('description') or details.get('text')}")
        properties = (await self.send("Runtime.getProperties", objectId=array["result"]["objectId"], ownProperties=True))["result"]
        elements = [p["value"]["objectId"] for p in properties if p["name"].isdigit() and "objectId" in p.get("value", {})]
        if len(elements) != 1:
            raise ToolError(f"Selector {target!r} matched {len(elements)} elements; it must match exactly one. Use a ref from browser_snapshot instead.")
        node = (await self.send("DOM.describeNode", objectId=elements[0]))["node"]
        return Element(self.id, node["backendNodeId"])

    async def frame_offset(self, session: str, scroll: bool = False) -> tuple[float, float]:
        """The offset of an out-of-process iframe's content in the page's viewport, optionally scrolling it into view."""
        if session == self.id:
            return 0.0, 0.0
        parent, frame = self.parents[session]
        owner = (await self.send("DOM.getFrameOwner", parent, frameId=frame))["backendNodeId"]
        if scroll:
            with contextlib.suppress(ProtocolError):
                await self.send("DOM.scrollIntoViewIfNeeded", parent, backendNodeId=owner)
        dx, dy = await self.frame_offset(parent, scroll)
        x, y, *_ = (await self.send("DOM.getBoxModel", parent, backendNodeId=owner))["model"]["content"]
        return x + dx, y + dy

    async def box(self, element: Element, scroll: bool = True) -> tuple[float, float, float, float]:
        """The element's bounding box (x, y, width, height) in the viewport, scrolled into view."""
        try:
            if scroll:
                if element.session != self.id:
                    await self.frame_offset(element.session, scroll=True)
                await self.send("DOM.scrollIntoViewIfNeeded", element.session, backendNodeId=element.backend)
            offset = await self.frame_offset(element.session)
            quads = (await self.send("DOM.getContentQuads", element.session, backendNodeId=element.backend))["quads"]
        except ProtocolError as error:
            if "not found" in str(error).lower() or "no node" in str(error).lower():
                raise ToolError("The element is no longer in the page. Take a new snapshot to get fresh refs.") from None
            raise ToolError(f"The element is not visible ({error})") from None
        quads = [q for q in quads if abs((q[2] - q[0]) * (q[5] - q[1])) > 0.5 or abs((q[4] - q[0]) * (q[5] - q[1])) > 0.5]
        if not quads:
            raise ToolError("The element is not visible (it has no size)")
        self.last_quads = [[v + offset[i % 2] for i, v in enumerate(q)] for q in quads]
        xs = [v for q in quads for v in q[0::2]]
        ys = [v for q in quads for v in q[1::2]]
        x, y = min(xs), min(ys)
        return x + offset[0], y + offset[1], max(xs) - x, max(ys) - y

    async def point(self, element: Element, check: bool = True) -> tuple[float, float]:
        """A point to click on the element, after scrolling it into view and checking nothing covers it."""
        for attempt in range(4):
            x, y, w, h = await self.box(element)
            px, py = x + w / 2, y + h / 2
            if not check or element.session != self.id or element.frame:
                return px, py
            # The middle of the bounding box can miss an element that is not a rectangle, such as a search result's
            # link around a title and a URL line: it may fall between them, on a thumbnail or a "Translate" link.
            # So the middles of the element's own boxes are tried too, largest first, as a person clicks the text.
            candidates = [(px, py)]
            viewport_w, viewport_h = self.browser.config.width, self.browser.config.height
            for quad in sorted(self.last_quads, key=lambda q: -abs((q[2] - q[0]) * (q[5] - q[1]))):
                qx, qy = sum(quad[0::2]) / 4, sum(quad[1::2]) / 4
                if 0 <= qx < viewport_w and 0 <= qy < viewport_h and all(abs(qx - cx) + abs(qy - cy) > 2 for cx, cy in candidates):
                    candidates.append((qx, qy))
            for cx, cy in candidates[:6]:
                covering = await self.covering(element, cx, cy)
                if covering is None:
                    return cx, cy
            await asyncio.sleep(0.25)
        raise ToolError(f"{covering} intercepts pointer events at the element.{await self.cover_hint(px, py)}")

    async def cover_hint(self, x: float, y: float) -> str:
        """What covers (x, y), as the snapshot shows it, and the controls in it that may dismiss it."""
        fallback = " Dismiss it first, for example with its close button or Escape."
        try:
            if not self.items:
                await self.snapshot()
            context = await self.world()
            # The covering element's ancestors, then the controls of the overlay it belongs to: its nearest positioned
            # ancestor of some size, such as a popover's or a banner's box, which often has no role of its own.
            found = await self.send("Runtime.evaluate", contextId=context, returnByValue=False, expression=f"""(() => {{
                const hit = document.elementFromPoint({x}, {y});
                const ancestors = [];
                let overlay = null;
                for (let n = hit; n && n !== document.body && ancestors.length < 25; n = n.parentElement) {{
                    ancestors.push(n);
                    const r = n.getBoundingClientRect(), position = getComputedStyle(n).position;
                    if (!overlay && ['fixed', 'absolute', 'sticky'].includes(position) && r.width >= 100 && r.height >= 40) overlay = n;
                }}
                const shown = e => {{ const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden'; }};
                const controls = overlay ? [...overlay.querySelectorAll('button, a[href], [role=button], [role=link], input[type=button], input[type=submit]')].filter(shown) : [];
                controls.sort((a, b) => (b.matches('button, [role=button], input') ? 1 : 0) - (a.matches('button, [role=button], input') ? 1 : 0));
                return {{ancestors, controls: controls.slice(0, 10), overlay: overlay ? [overlay] : []}};
            }})()""")
            groups = {}
            for group in (await self.send("Runtime.getProperties", objectId=found["result"]["objectId"], ownProperties=True))["result"]:
                if "objectId" not in group.get("value", {}):
                    continue
                entries = (await self.send("Runtime.getProperties", objectId=group["value"]["objectId"], ownProperties=True))["result"]
                ids = [e["value"]["objectId"] for e in sorted((e for e in entries if e["name"].isdigit()), key=lambda e: int(e["name"]))]
                groups[group["name"]] = [self.ref_ids.get(("", (await self.send("DOM.describeNode", objectId=i))["node"]["backendNodeId"]))
                                         for i in ids]
        except (ProtocolError, TimeoutError, KeyError):
            return fallback
        items = lambda refs: [item for ref in refs if ref and (item := find_item(self.items, ref))]
        describe = lambda c: f'{c.role} "{c.name}" [ref={c.ref}]' if c.name else f"{c.role} [ref={c.ref}]"
        # A dialog the cover is part of says it best; else the overlay's own controls.
        dialog = next((item for item in items(groups.get("ancestors", [])) if item.role in ("dialog", "alertdialog")), None)
        if dialog:
            listed = ", ".join(describe(c) for c in controls(dialog))
            where = f'{dialog.role}{f" {dialog.name!r}" if dialog.name else ""} [ref={dialog.ref}]'
            return f" It is part of {where}" + (f", whose controls are: {listed}." if listed else ".") + " Dismiss it first."
        listed = ", ".join(describe(c) for c in items(groups.get("controls", [])))
        if listed:
            return f" It is part of an overlay whose controls are: {listed}. Close or dismiss it first."
        return fallback

    async def covering(self, element: Element, x: float, y: float) -> str | None:
        """What covers `element` at (x, y), or None when a click there reaches it."""
        try:
            return await self.call(element, COVERS, x, y)
        except (ProtocolError, ToolError):
            return None

    # Input.

    async def mouse_to(self, x: float, y: float, steps: int | None = None, buttons: int = 0, button: str = "none"):
        sx, sy = self.mouse
        steps = steps or max(2, min(8, int(math.hypot(x - sx, y - sy) / 120)))
        for step in range(1, steps + 1):
            t = step / steps
            ease = t * t * (3 - 2 * t)
            jitter = 0 if step == steps else random.uniform(-1.5, 1.5)
            await self.send("Input.dispatchMouseEvent", type="mouseMoved", x=sx + (x - sx) * ease + jitter,
                            y=sy + (y - sy) * ease + jitter, buttons=buttons, button=button)
            await asyncio.sleep(random.uniform(0.005, 0.02))
        self.mouse = (x, y)

    async def click_at(self, x: float, y: float, button: str = "left", count: int = 1, modifiers: int = 0):
        x, y = x + random.uniform(-1.5, 1.5), y + random.uniform(-1.5, 1.5)
        await self.mouse_to(x, y)
        flag = {"left": 1, "right": 2, "middle": 4}[button]
        for click in range(1, count + 1):
            for kind in ("mousePressed", "mouseReleased"):
                await self.send("Input.dispatchMouseEvent", type=kind, x=x, y=y, button=button, clickCount=click,
                                buttons=flag if kind == "mousePressed" else 0, modifiers=modifiers)
                await asyncio.sleep(random.uniform(0.03, 0.08))

    async def click(self, element: Element, button: str = "left", count: int = 1, modifiers: list[str] = ()) -> list[str]:
        notes = []
        try:
            x, y = await self.point(element)
        except ToolError as error:
            # Custom dropdowns and checkboxes hide their real input at zero size inside the control a person clicks.
            if "has no size" not in str(error) or not (container := await self.sized_ancestor(element)):
                raise
            x, y = await self.point(container)
            notes.append("The element has no size, so its enclosing control was clicked instead.")
        await self.hold(modifiers, lambda mask: self.click_at(x, y, button, count, mask))
        return notes

    async def sized_ancestor(self, element: Element, levels: int = 3) -> Element | None:
        """The nearest of the element's first ancestors that has a size on screen."""
        with contextlib.suppress(ProtocolError, ToolError, KeyError):
            params = {"backendNodeId": element.backend}
            node = (await self.send("DOM.describeNode", element.session, **params, depth=0))["node"]
            for _ in range(levels):
                parent = await self.call(Element(element.session, node["backendNodeId"], element.frame),
                                         "function() { return this.parentElement; }", isolated=False, by_value=False)
                if "objectId" not in parent:
                    return None
                node = (await self.send("DOM.describeNode", element.session, objectId=parent["objectId"]))["node"]
                candidate = Element(element.session, node["backendNodeId"], element.frame)
                with contextlib.suppress(ToolError):
                    _, _, w, h = await self.box(candidate)
                    if w >= 2 and h >= 2:
                        return candidate
        return None

    async def drag_xy(self, sx: float, sy: float, ex: float, ey: float):
        await self.mouse_to(sx, sy)
        await self.send("Input.dispatchMouseEvent", type="mousePressed", x=sx, y=sy, button="left", buttons=1, clickCount=1)
        await self.mouse_to(ex, ey, steps=12, buttons=1, button="left")
        await self.send("Input.dispatchMouseEvent", type="mouseReleased", x=ex, y=ey, button="left", buttons=0, clickCount=1)

    async def wheel(self, dx: float, dy: float):
        x, y = self.mouse
        await self.send("Input.dispatchMouseEvent", type="mouseWheel", x=x, y=y, deltaX=dx, deltaY=dy)

    async def hover(self, element: Element):
        x, y = await self.point(element, check=False)
        await self.mouse_to(x, y)

    async def drag(self, start: Element, end: Element):
        sx, sy = await self.point(start, check=False)
        await self.mouse_to(sx, sy)
        await self.send("Input.setInterceptDrags", enabled=True)
        intercepted = asyncio.ensure_future(self.cdp.wait_for(
            lambda m, p, s: p if m == "Input.dragIntercepted" and s == self.id else None, timeout=1.5))
        try:
            await self.send("Input.dispatchMouseEvent", type="mousePressed", x=sx, y=sy, button="left", buttons=1, clickCount=1)
            ex, ey = await self.point(end, check=False)
            await self.mouse_to(sx + 8, sy + 8, steps=2, buttons=1, button="left")
            data = None
            with contextlib.suppress(TimeoutError):
                data = (await asyncio.wait_for(asyncio.shield(intercepted), 0.5))["data"]
            if data is not None:  # An HTML5 drag: Chrome hands its data over to be dropped.
                for kind in ("dragEnter", "dragOver", "drop"):
                    await self.send("Input.dispatchDragEvent", type=kind, x=ex, y=ey, data=data)
                    await asyncio.sleep(0.05)
                self.mouse = (ex, ey)
            else:  # Pointer-event drag libraries follow the mouse.
                await self.mouse_to(ex, ey, steps=10, buttons=1, button="left")
            await self.send("Input.dispatchMouseEvent", type="mouseReleased", x=ex, y=ey, button="left", buttons=0, clickCount=1)
        finally:
            intercepted.cancel()
            with contextlib.suppress(ProtocolError):
                await self.send("Input.setInterceptDrags", enabled=False)

    async def drop(self, element: Element, files: list[str], data: dict[str, str]):
        x, y = await self.point(element, check=False)
        await self.mouse_to(x, y)
        payload = {"items": [{"mimeType": mime, "data": value} for mime, value in data.items()],
                   "files": files, "dragOperationsMask": 1 | 2 | 16}
        for kind in ("dragEnter", "dragOver", "drop"):
            await self.send("Input.dispatchDragEvent", type=kind, x=x, y=y, data=payload)
            await asyncio.sleep(0.05)

    async def hold(self, modifiers, action):
        """Run `action(mask)` with `modifiers` held down."""
        names = [keys.CONTROL_OR_META if m == "ControlOrMeta" else m for m in modifiers]
        held: list[str] = []
        try:
            for name in names:
                held.append(name)
                await self.key_event("rawKeyDown", keys.KEYS[name], keys.mask(held))
            return await action(keys.mask(held))
        finally:
            for name in reversed(held):
                held.remove(name)
                await self.key_event("keyUp", keys.KEYS[name], keys.mask(held))

    async def key_event(self, kind: str, key: keys.Key, modifiers: int, text: str | None = None, commands: list[str] | None = None):
        params = {"type": kind, "key": key.key, "code": key.code, "windowsVirtualKeyCode": key.keycode,
                  "nativeVirtualKeyCode": key.keycode, "modifiers": modifiers, "location": key.location}
        if text:
            params |= {"text": text, "unmodifiedText": text}
        if commands:
            params["commands"] = commands
        await self.send("Input.dispatchKeyEvent", **params)

    async def press(self, combo: str):
        try:
            modifiers, key = keys.parse(combo)
        except (ValueError, KeyError) as error:
            raise ToolError(str(error)) from None

        async def tap(mask: int):
            text = key.text if not mask & (keys.MODIFIERS["Control"] | keys.MODIFIERS["Meta"] | keys.MODIFIERS["Alt"]) else None
            if text and mask & keys.MODIFIERS["Shift"] and len(text) == 1:
                text = text.upper()
            commands = None
            if mask & keys.MODIFIERS["Meta"] and keys.CONTROL_OR_META == "Meta" and key.key.lower() in keys.MAC_COMMANDS:
                commands = [keys.MAC_COMMANDS[key.key.lower()]]
            if key.code == "" and text:  # A character not on the keyboard.
                await self.send("Input.insertText", text=text)
                return
            await self.key_event("keyDown" if text else "rawKeyDown", key, mask, text, commands)
            await asyncio.sleep(random.uniform(0.02, 0.05))
            await self.key_event("keyUp", key, mask)

        await self.hold(modifiers, tap)

    async def type(self, element: Element, text: str, slowly: bool = False, submit: bool = False):
        await self.focus(element)
        if not await self.call(element, SELECT_ALL):
            raise ToolError("The element is not an <input>, <textarea> or [contenteditable] element")
        if not text:
            await self.press("Delete")
        elif slowly:
            for char in text:
                await self.press("Enter" if char == "\n" else char if char in keys.KEYS else char)
                await asyncio.sleep(random.uniform(0.03, 0.12))
        else:
            await self.send("Input.insertText", text=text)
        if submit:
            await self.press("Enter")

    async def focus(self, element: Element):
        await self.box(element)  # Scrolls it into view and fails clearly when it is gone or hidden.
        try:
            await self.send("DOM.focus", element.session, backendNodeId=element.backend)
        except ProtocolError as error:
            raise ToolError(f"The element cannot be focused ({error})") from None

    async def select(self, element: Element, values: list[str]) -> list[str]:
        await self.focus(element)
        return await self.call(element, SELECT_OPTIONS, values)

    async def set_checked(self, element: Element, checked: bool):
        state = await self.call(element, STATE)
        if bool(state["checked"]) != checked:
            if state["type"] == "radio" and not checked:
                raise ToolError("A radio button cannot be unchecked; check another option instead")
            await self.click(element)

    async def set_value(self, element: Element, value: str):
        await self.focus(element)
        await self.call(element, SET_VALUE, value)

    # Snapshots.

    async def snapshot(self, boxes: bool = False) -> list[Item | str]:
        frame = await self.frame_tree(self.id, None, depth=0, cache={})
        self.items = build(frame, self.ref_for)
        refs = {}
        for (session, frame_id), prefix in self.prefixes.items():
            refs[prefix] = (session, frame_id)
        for (prefix, backend), ref in self.ref_ids.items():
            if prefix in refs:
                session, frame_id = refs[prefix]
                self.refs[ref] = Element(session, backend, frame_id)
        return self.items

    def prefix(self, session: str, frame: str | None) -> str:
        key = (session, frame)
        if key not in self.prefixes:
            self.prefixes[key] = "" if session == self.id and frame is None else f"f{next(self.prefix_counter)}"
        return self.prefixes[key]

    async def frame_tree(self, session: str, frame: str | None, depth: int, cache: dict, offset=(0.0, 0.0)) -> Frame:
        """The accessibility tree of a frame with its child frames', and each element's cursor and box."""
        params = {"frameId": frame} if frame else {}
        nodes = (await self.send("Accessibility.getFullAXTree", session, timeout=30, **params))["nodes"]
        cursors, displays, boxes = await self.layout(session, frame, offset, cache)
        result = Frame(self.prefix(session, frame), nodes, cursors, displays, boxes)
        if depth >= 4:
            return result
        iframes = 0
        for node in nodes:
            if node.get("role", {}).get("value") not in ("Iframe", "IframePresentational") or node.get("ignored"):
                continue
            backend = node.get("backendDOMNodeId")
            box = boxes.get(backend)
            if backend is None or (box and (box[2] < 2 or box[3] < 2)):  # Tracking pixels.
                continue
            iframes += 1
            if iframes > 25:
                break
            with contextlib.suppress(ProtocolError, TimeoutError, KeyError):
                child = (await self.send("DOM.describeNode", session, backendNodeId=backend))["node"].get("frameId")
                if not child:
                    continue
                child_offset = (box[0], box[1]) if box else offset
                child_session = self.frames.get(child)
                result.children[backend] = await self.frame_tree(child_session or session, None if child_session else child,
                                                                 depth + 1, cache, child_offset)
        return result

    async def layout(self, session: str, frame: str | None, offset, cache: dict) -> tuple[dict, dict, dict]:
        """Computed cursors and displays, and viewport boxes of a frame's elements, from one DOM snapshot of its process."""
        if session not in cache:
            try:
                cache[session] = await self.send("DOMSnapshot.captureSnapshot", session, timeout=30, computedStyles=["cursor", "display"])
            except (ProtocolError, TimeoutError):
                cache[session] = None
        if not (snap := cache[session]):
            return {}, {}, {}
        strings = snap["strings"]
        wanted = frame or (self.frame if session == self.id else self.parents.get(session, (None, None))[1])
        cursors, displays, boxes = {}, {}, {}
        for document in snap["documents"]:
            if strings[document["frameId"]] != wanted:
                continue
            backend_ids = document["nodes"]["backendNodeId"]
            layout = document["layout"]
            sx, sy = document.get("scrollOffsetX", 0), document.get("scrollOffsetY", 0)
            for node_index, style, bounds in zip(layout["nodeIndex"], layout["styles"], layout["bounds"]):
                backend = backend_ids[node_index]
                if style and style[0] >= 0:
                    cursors[backend] = strings[style[0]]
                if len(style) > 1 and style[1] >= 0:
                    displays[backend] = strings[style[1]]
                boxes[backend] = (bounds[0] - sx + offset[0], bounds[1] - sy + offset[1], bounds[2], bounds[3])
            break
        return cursors, displays, boxes

    async def title(self) -> str:
        with contextlib.suppress(ProtocolError, TimeoutError, ConnectionError):
            info = (await self.cdp.send("Target.getTargetInfo", targetId=self.target, timeout=5))["targetInfo"]
            self.url = info.get("url") or self.url
            title = info.get("title", "")
            # Chrome titles an untitled page with its URL; the page's own title is empty.
            bare = re.sub(r"^[a-z][a-z0-9+.-]*://", "", self.url)
            return "" if title and title in (self.url, bare, bare.removeprefix("www.")) else title
        return ""

    # Other page operations.

    async def screenshot(self, kind: str, full_page: bool, element: Element | None) -> tuple[bytes, bytes, tuple[int, int]]:
        """The screenshot at full resolution, a copy scaled down for the model if needed, and its CSS size."""
        params = {"format": kind}
        if kind in ("jpeg", "webp"):
            params["quality"] = 85
        metrics = await self.send("Page.getLayoutMetrics")
        viewport = metrics["cssVisualViewport"]
        if element:
            x, y, w, h = await self.box(element)
            clip = {"x": x + viewport["pageX"], "y": y + viewport["pageY"], "width": w, "height": h}
            params["captureBeyondViewport"] = True
        elif full_page:
            size = metrics["cssContentSize"]
            clip = {"x": 0, "y": 0, "width": size["width"], "height": min(size["height"], 16_000)}
            params["captureBeyondViewport"] = True
        else:
            clip = {"x": viewport["pageX"], "y": viewport["pageY"], "width": viewport["clientWidth"], "height": viewport["clientHeight"]}
        if clip["width"] < 1 or clip["height"] < 1:
            raise ToolError("Nothing to capture: the area has no size")
        full = base64.b64decode((await self.send("Page.captureScreenshot", timeout=60, clip=clip | {"scale": 1}, **params))["data"])
        width, height = clip["width"], clip["height"]
        scale = min(1.0, math.sqrt(4_000_000 / (width * height)), 7_900 / max(width, height))
        small = full if scale >= 1 else base64.b64decode(
            (await self.send("Page.captureScreenshot", timeout=60, clip=clip | {"scale": scale}, **params))["data"])
        return full, small, (round(width), round(height))

    async def evaluate(self, function: str, element: Element | None, timeout: float = 30):
        source = function.strip()
        if element:  # `(element) => ...` gets the element as its argument, in the page's own world.
            try:
                object_id = (await self.send("DOM.resolveNode", element.session, backendNodeId=element.backend))["object"]["objectId"]
            except ProtocolError:
                raise ToolError("The element is no longer in the page. Take a new snapshot to get fresh refs.") from None
            result = await self.send("Runtime.callFunctionOn", element.session, timeout=timeout + 5, functionDeclaration=source,
                                     objectId=object_id, arguments=[{"objectId": object_id}], awaitPromise=True,
                                     returnByValue=False, userGesture=True)
            if "exceptionDetails" in result:
                details = result["exceptionDetails"]
                raise ToolError((details.get("exception") or {}).get("description") or details.get("text", "error"))
            return self.unwrap(await self.by_value(result["result"], element.session))
        expression = f"(async () => {{ const __f = ({source}); return typeof __f === 'function' ? await __f() : __f; }})()"
        params = {"expression": expression, "awaitPromise": True, "returnByValue": False, "userGesture": True,
                  "timeout": int(timeout * 1000)}  # Chrome stops synchronous scripts that run longer.
        started = time.monotonic()
        try:
            result = await self.send("Runtime.evaluate", timeout=timeout + 5, extra=params)
            if "exceptionDetails" in result and "SyntaxError" in json.dumps(result["exceptionDetails"]):  # Statements, not an expression.
                result = await self.send("Runtime.evaluate", timeout=timeout + 5, extra=params | {"expression": source, "replMode": True})
        except (ProtocolError, TimeoutError) as error:
            if time.monotonic() - started >= timeout - 0.5:
                raise ToolError(f"The script did not finish in {timeout:.0f}s and was stopped") from None
            raise ToolError(f"Evaluation failed: {error}") from None
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            raise ToolError((details.get("exception") or {}).get("description") or details.get("text", "error"))
        return self.unwrap(await self.by_value(result["result"], self.id))

    async def by_value(self, obj: dict, session: str) -> dict:
        """A JSON copy of a RemoteObject; nodes and functions stay as their descriptions."""
        if "objectId" not in obj or obj.get("subtype") == "node" or obj.get("type") == "function":
            return obj
        result = await self.send("Runtime.callFunctionOn", session, objectId=obj["objectId"], returnByValue=True,
                                 functionDeclaration="function() { return this; }")
        return obj if "exceptionDetails" in result else result["result"]

    @staticmethod
    def unwrap(obj: dict):
        if obj.get("type") == "undefined":
            return None
        if "value" in obj:
            return obj["value"]
        if "unserializableValue" in obj:
            return obj["unserializableValue"]
        return obj.get("description", obj.get("type"))

    async def handle_dialog(self, accept: bool, prompt: str | None):
        if not self.dialog:
            raise ToolError("No dialog visible")
        params = {"accept": accept} | ({"promptText": prompt} if prompt is not None else {})
        await self.send("Page.handleJavaScriptDialog", self.dialog["session"], **params)
        self.dialog = None
        if not self.chooser:
            self.modal.clear()

    async def upload(self, paths: list[str] | None):
        if not self.chooser:
            raise ToolError("No file chooser visible")
        chooser, self.chooser = self.chooser, None
        if not self.dialog:
            self.modal.clear()
        if not paths:
            return
        if chooser.get("mode") == "selectSingle" and len(paths) > 1:
            raise ToolError("The file input accepts a single file")
        files = self.browser.stage(paths)
        await self.send("DOM.setFileInputFiles", chooser["session"], files=files, backendNodeId=chooser["backendNodeId"])

    async def resize(self, width: int, height: int):
        window = (await self.cdp.send("Browser.getWindowForTarget", targetId=self.target))["windowId"]
        with contextlib.suppress(ProtocolError):
            await self.cdp.send("Browser.setWindowBounds", windowId=window, bounds={"windowState": "normal"})
        await self.cdp.send("Browser.setWindowBounds", windowId=window, bounds={"width": int(width), "height": int(height)})
        await asyncio.sleep(0.2)
        inner = await self.eval("[innerWidth, innerHeight]", isolated=True)
        if inner and (inner[0], inner[1]) != (width, height):  # Window decorations take room; correct for them.
            await self.cdp.send("Browser.setWindowBounds", windowId=window, bounds={
                "width": int(2 * width - inner[0]), "height": int(2 * height - inner[1])})

    async def emulate_media(self, **settings):
        self.media |= settings
        self.media = {k: v for k, v in self.media.items() if v is not None}
        features = [{"name": name, "value": self.media[key]} for key, name in (
            ("colorScheme", "prefers-color-scheme"), ("contrast", "prefers-contrast"), ("forcedColors", "forced-colors"),
            ("reducedMotion", "prefers-reduced-motion")) if key in self.media]
        await self.send("Emulation.setEmulatedMedia", media=self.media.get("media", ""), features=features)

    async def back(self):
        history = await self.send("Page.getNavigationHistory")
        if history["currentIndex"] <= 0:
            raise ToolError("There is no previous page in this tab's history")
        navigations = self.navigations
        self.loading = True
        await self.send("Page.navigateToHistoryEntry", entryId=history["entries"][history["currentIndex"] - 1]["id"])
        await self.after_action(navigations - 1, len(self.downloads))  # Always settle as a navigation.

    async def html(self) -> str:
        while True:
            try:
                return await self.eval(SERIALIZE, isolated=True, timeout=30)
            except ProtocolError:  # Mid-navigation.
                await asyncio.sleep(POLL)

    async def collect(self):
        """Log the page errors the isolated world has relayed (with full capture on, Chrome reports them itself)."""
        if self.runtime or self.closed or self.dialog:
            return
        with contextlib.suppress(ProtocolError, TimeoutError, ConnectionError, KeyError):
            for text, location in await self.eval(COLLECT, isolated=True, timeout=5) or []:
                self.log(Message("error", "ERROR", text, location))

    def new_messages(self) -> list[Message]:
        fresh = self.messages[self.console_reported:]
        self.console_reported = len(self.messages)
        return fresh

    def console(self, level: str, all: bool) -> list[Message]:
        threshold = LEVELS[level]
        return [m for m in self.messages[0 if all else self.console_start:] if LEVELS.get(m.level, 1) >= threshold]

    def network(self, static: bool, all: bool, filter: str | None) -> list[Request]:
        pattern = re.compile(filter) if filter else None
        return [r for r in self.requests[0 if all else self.network_start:]
                if (static or not (r.type in STATIC_TYPES and not r.failed and (r.status or 0) < 400))
                and (pattern is None or pattern.search(r.url))]

    async def request_detail(self, index: int, part: str | None) -> tuple[str, bytes | None]:
        """A request's details as text, or for a binary body (text, bytes)."""
        entry = next((r for r in self.requests if r.index == index), None)
        if entry is None:
            raise ToolError(f"Request #{index} not found. Use browser_network_requests to see available indexes.")
        headers = lambda h: "\n".join(f"{k}: {v}" for k, v in h.items()) or "(none)"
        sections = {}
        if part in (None, "request-headers"):
            sections["Request headers"] = headers(entry.headers)
        if part in (None, "request-body"):
            body = entry.post
            if body is None and entry.has_post:
                with contextlib.suppress(ProtocolError):
                    body = (await self.send("Network.getRequestPostData", entry.session, requestId=entry.id))["postData"]
            sections["Request body"] = body if body is not None else "(none)"
        if part in (None, "response-headers"):
            sections["Response headers"] = headers(entry.response_headers) if entry.status is not None else "(no response)"
        binary = None
        if part in (None, "response-body"):
            try:
                body = await self.send("Network.getResponseBody", entry.session, requestId=entry.id)
                data = base64.b64decode(body["body"]) if body.get("base64Encoded") else body["body"].encode()
                try:
                    text = data.decode("utf-8")
                    if "\x00" in text:
                        raise UnicodeDecodeError("utf-8", data, 0, 1, "binary")
                    sections["Response body"] = text
                except UnicodeDecodeError:
                    binary = data
                    sections["Response body"] = f"(binary, {len(data)} bytes, {entry.mime or 'unknown type'})"
            except ProtocolError as error:
                sections["Response body"] = f"(unavailable: {error})"
        title = f"{entry.line()}\n- Type: {entry.type}" + (f"\n- MIME type: {entry.mime}" if entry.mime else "")
        if part:
            return next(iter(sections.values())), binary
        return title + "\n\n" + "\n\n".join(f"#### {k}\n{v}" for k, v in sections.items()), binary


class Session:
    """Chrome, started on first use, with the tabs the model works in."""

    def __init__(self, config: Config | None = None):
        self.config = config or Config()
        self.cdp: CDP | None = None
        self.start_lock = asyncio.Lock()
        self.all_tabs: list[Tab] = []  # Every caller's tabs, in order.
        self.currents: dict[str, Tab] = {}  # Caller -> its current tab.
        self.last_caller = ""  # The caller of the latest tool call, for events no tab claims.
        self.targets: dict[str, Tab] = {}
        self.owners: dict[str, Tab] = {}  # Any session (page or iframe) -> its tab.
        self.attached: dict[str, asyncio.Event] = {}
        self.contexts: dict[str, str] = {}  # Isolated context name -> browserContextId.
        self.downloads: dict[str, dict] = {}
        self.uploads = itertools.count(1)
        self.creating = 0  # Tabs being opened by us; any other page without an opener is a popup.
        self.profile = ""
        self.temporary = True
        self.lock_file = None
        self.output_dir = os.path.abspath(os.path.expanduser(self.config.output_dir or os.path.join(tempfile.gettempdir(), "browse-anything")))

    # The calling session's view.

    @property
    def tabs(self) -> list["Tab"]:
        """The caller's tabs, in order; the indexes the model sees."""
        return self.tabs_of(CALLER.get())

    def tabs_of(self, caller: str) -> list["Tab"]:
        return [tab for tab in self.all_tabs if tab.caller == caller]

    @property
    def current(self) -> "Tab | None":
        return self.currents.get(CALLER.get())

    @current.setter
    def current(self, tab: "Tab | None"):
        if tab is None:
            self.currents.pop(CALLER.get(), None)
        else:
            self.currents[CALLER.get()] = tab

    # Chrome.

    async def ensure(self) -> CDP:
        async with self.start_lock:
            if self.cdp and self.cdp.alive:
                return self.cdp
            self.reset()
            self.prepare_profile()
            self.cdp = await launch(self.profile, self.config.width, self.config.height)
            self.cdp.on(self.dispatch)
            await self.cdp.send("Target.setAutoAttach", autoAttach=True, waitForDebuggerOnStart=True, flatten=True, filter=[{"type": "page"}])
            await self.allow_downloads(None)
            return self.cdp

    def prepare_profile(self):
        if self.profile:
            return
        if self.config.profile:
            self.profile, self.temporary = os.path.abspath(os.path.expanduser(self.config.profile)), False
            os.makedirs(self.profile, exist_ok=True)
            self.lock_file = open(os.path.join(self.profile, ".browse-anything.lock"), "w")
            try:
                fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.lock_file.close()
                self.lock_file, self.profile = None, ""
                raise ToolError(f"The Chrome profile {self.config.profile} is in use by another browse-anything server") from None
        else:
            self.profile = tempfile.mkdtemp(prefix="browse-anything-")
        # Chrome's lock is keyed to a pid in the sandbox's pid namespace, so a stale one can look alive; our own
        # lock above already guarantees no other Chrome uses this profile.
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(os.path.join(self.profile, name))

    def reset(self):
        for tab in self.targets.values():
            tab.closed = True
        self.all_tabs, self.currents, self.targets, self.owners, self.attached, self.contexts = [], {}, {}, {}, {}, {}

    async def allow_downloads(self, context: str | None):
        params = {"behavior": "allowAndName", "downloadPath": os.path.join(self.profile, "downloads"), "eventsEnabled": True}
        os.makedirs(params["downloadPath"], exist_ok=True)
        await self.cdp.send("Browser.setDownloadBehavior", **params, **({"browserContextId": context} if context else {}))

    def dispatch(self, method: str, params: dict, session: str | None):
        if method == "Target.attachedToTarget":
            asyncio.ensure_future(self.adopt(params, session))
        elif method == "Target.detachedFromTarget":
            self.detach(params["sessionId"], session)
        elif method == "Browser.downloadWillBegin":
            tab = next((t for t in self.targets.values() if params.get("frameId") in (t.frame, *t.frames)),
                       self.currents.get(self.last_caller))
            self.downloads[params["guid"]] = params | {"tab": tab, "state": "inProgress", "done": asyncio.Event()}
            if tab:
                tab.downloads.add(params["guid"])
        elif method == "Browser.downloadProgress" and params["state"] != "inProgress":
            self.finish_download(params)
        elif session and (tab := self.owners.get(session)):
            tab.handle(method, params, session)

    async def adopt(self, params: dict, parent: str | None):
        info, session = params["targetInfo"], params["sessionId"]
        try:
            if parent is None and info["type"] == "page":
                opener = self.targets.get(info.get("openerId"))
                context = next((name for name, id in self.contexts.items() if id == info.get("browserContextId")), None)
                tab = Tab(self, info["targetId"], session, context, info.get("openerId"), hidden=bool(opener and opener.hidden))
                self.targets[tab.target] = tab
                self.owners[session] = tab
                sent = tab.setup()
                if params.get("waitingForDebugger"):
                    sent.append(self.cdp.post("Runtime.runIfWaitingForDebugger", session))
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.gather(*sent, return_exceptions=True), 30)
                if opener and opener.hidden:
                    with contextlib.suppress(ProtocolError, ConnectionError):
                        await self.cdp.send("Target.closeTarget", targetId=tab.target)
                elif opener or not self.creating:  # A popup: the model sees it in the tab list.
                    # This runs for Chrome's event, outside any tool call: the popup belongs to its opener's caller.
                    tab.caller = opener.caller if opener else self.last_caller
                    self.all_tabs.append(tab)
                    if reporter := opener or self.currents.get(tab.caller):
                        reporter.events.append(f"New tab {self.tabs_of(tab.caller).index(tab)} opened by the page "
                                               "(select it with browser_tabs)")
                tab.ready.set()
                self.attached.setdefault(tab.target, asyncio.Event()).set()
            elif parent and info["type"] == "iframe" and (tab := self.owners.get(parent)):
                tab.frames[info["targetId"]] = session
                tab.parents[session] = (parent, info["targetId"])
                self.owners[session] = tab
                await tab.setup_frame(session)
                if params.get("waitingForDebugger"):
                    await self.cdp.send("Runtime.runIfWaitingForDebugger", session)
            elif params.get("waitingForDebugger"):
                await self.cdp.send("Runtime.runIfWaitingForDebugger", session)
        except (ProtocolError, ConnectionError):
            pass

    def detach(self, session: str, parent: str | None):
        tab = self.owners.pop(session, None)
        if tab is None:
            return
        if session == tab.id:
            tab.closed = True
            self.targets.pop(tab.target, None)
            for child in tab.parents:
                self.owners.pop(child, None)
            if tab in self.all_tabs:
                own = self.tabs_of(tab.caller)
                index = own.index(tab)
                self.all_tabs.remove(tab)
                own.remove(tab)
                if self.currents.get(tab.caller) is tab:
                    if own:
                        self.currents[tab.caller] = own[min(index, len(own) - 1)]
                    else:
                        del self.currents[tab.caller]
        else:
            tab.frames = {frame: child for frame, child in tab.frames.items() if child != session}
            tab.parents.pop(session, None)

    # Tabs.

    async def new_tab(self, context: str | None = None, hidden: bool = False, select: bool = True) -> Tab:
        cdp = await self.ensure()
        params = {"url": "about:blank", "newWindow": True, "width": self.config.width, "height": self.config.height}
        if context:
            if context not in self.contexts:
                self.contexts[context] = (await cdp.send("Target.createBrowserContext", disposeOnDetach=True))["browserContextId"]
                await self.allow_downloads(self.contexts[context])
            params["browserContextId"] = self.contexts[context]
        if hidden:
            params |= {"width": 1280, "height": 900}
        self.creating += 1
        try:
            target = (await cdp.send("Target.createTarget", **params))["targetId"]
            event = self.attached.setdefault(target, asyncio.Event())
            await asyncio.wait_for(event.wait(), 15)
        finally:
            self.creating -= 1
        tab = self.targets[target]
        tab.hidden = hidden
        if hidden:
            with contextlib.suppress(ProtocolError):
                await tab.send("Page.setInterceptFileChooserDialog", enabled=False)
        else:
            # Chrome's window size includes room for its toolbars; size the window so the viewport is as configured.
            with contextlib.suppress(ProtocolError, TimeoutError):
                await tab.resize(self.config.width, self.config.height)
            tab.caller = CALLER.get()
            self.all_tabs.append(tab)
            if select:
                await self.select(tab)
        return tab

    async def select(self, tab: Tab):
        self.current = tab
        with contextlib.suppress(ProtocolError):
            await tab.send("Page.bringToFront")

    async def tab(self) -> Tab:
        """The current tab, opening one if there is none."""
        if self.cdp and self.cdp.alive and self.current and not self.current.closed:
            return self.current
        if self.cdp and self.cdp.alive and self.tabs:
            await self.select(self.tabs[-1])
            return self.current
        return await self.new_tab()

    async def close_tab(self, tab: Tab):
        with contextlib.suppress(ProtocolError, ConnectionError):
            await self.cdp.send("Target.closeTarget", targetId=tab.target)
        self.detach(tab.id, None)

    async def expire_idle(self) -> list["Tab"]:
        """Close the tabs, of any caller, no tool has used for `tab_idle_minutes`; returns them."""
        limit = self.config.tab_idle_minutes * 60
        if not limit or not self.cdp or not self.cdp.alive:
            return []
        stale = [tab for tab in self.all_tabs if time.monotonic() - tab.used > limit and not tab.lock.locked()]
        for tab in stale:
            await self.close_tab(tab)
        if not self.all_tabs:
            for context in list(self.contexts.values()):
                with contextlib.suppress(ProtocolError, ConnectionError):
                    await self.cdp.send("Target.disposeBrowserContext", browserContextId=context)
            self.contexts = {}
        return stale

    async def close_all(self):
        """Close the caller's tabs; other callers' tabs stay open."""
        for tab in list(self.tabs):
            await self.close_tab(tab)
        if self.all_tabs:
            return
        for context in list(self.contexts.values()):
            with contextlib.suppress(ProtocolError, ConnectionError):
                await self.cdp.send("Target.disposeBrowserContext", browserContextId=context)
        self.contexts = {}

    # Files.

    def stage(self, paths: list[str]) -> list[str]:
        """Copy files where the sandboxed Chrome can read them: its profile, since bwrap hides the real /tmp."""
        directory = os.path.join(self.profile, "uploads", str(next(self.uploads)))
        os.makedirs(directory, exist_ok=True)
        staged = []
        for path in paths:
            source = os.path.abspath(os.path.expanduser(path))
            if not os.path.isfile(source):
                raise ToolError(f"File not found: {path}")
            target = os.path.join(directory, os.path.basename(source))
            shutil.copy2(source, target)
            staged.append(target)
        return staged

    def output_path(self, name: str) -> str:
        path = name if os.path.isabs(name) else os.path.join(self.output_dir, name)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        return path

    def finish_download(self, params: dict):
        download = self.downloads.get(params["guid"])
        if not download:
            return
        download["state"] = params["state"]
        tab = download["tab"]
        name = download.get("suggestedFilename") or "download"
        if params["state"] == "completed":
            source = os.path.join(self.profile, "downloads", params["guid"])
            base, extension = os.path.splitext(name)
            target = self.output_path(name)
            for n in itertools.count(1):
                if not os.path.exists(target):
                    break
                target = self.output_path(f"{base} ({n}){extension}")
            with contextlib.suppress(OSError):
                shutil.move(source, target)
                download["path"] = target
                if tab:
                    tab.events.append(f"Downloaded file {name} to {target}")
        elif tab:
            tab.events.append(f"Download of {name} was canceled")
        download["done"].set()

    async def finish_downloads(self, tab: Tab, timeout: float = 60):
        pending = [d["done"].wait() for guid, d in self.downloads.items() if guid in tab.downloads and d["state"] == "inProgress"]
        if pending:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.gather(*pending), timeout)
            for guid, download in self.downloads.items():
                if guid in tab.downloads and download["state"] == "inProgress":
                    tab.events.append(f"Downloading {download.get('suggestedFilename')} is still in progress")

    # Reading pages for `browse`.

    async def render(self, url: str) -> tuple[str, int, str, list[dict], str]:
        """Load `url` in a hidden tab, past any challenge; return its URL, status, HTML, cookies and user agent."""
        tab = await self.new_tab(hidden=True)
        try:
            await tab.navigate(url)
            await tab.scroll_through()
            html = await tab.html()
            host = urlsplit(tab.url).hostname or ""
            cookies = [c for c in (await self.cdp.send("Storage.getCookies"))["cookies"]
                       if f".{host}".endswith("." + c["domain"].lstrip("."))]
            agent = (await self.cdp.send("Browser.getVersion"))["userAgent"]
            return tab.url, tab.status or 200, html, cookies, agent
        finally:
            with contextlib.suppress(ProtocolError, ConnectionError):
                await self.cdp.send("Target.closeTarget", targetId=tab.target)

    def close(self):
        if self.cdp:
            self.cdp.kill(grace=0 if self.temporary else 5)
        if self.temporary and self.profile:
            shutil.rmtree(self.profile, ignore_errors=True)
        if self.lock_file:
            self.lock_file.close()
