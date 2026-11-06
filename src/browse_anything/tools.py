"""MCP tools with Playwright MCP's names, arguments and response layout, on browse-anything's Chrome.

Responses use Playwright MCP's sections (`### Result`, `### Page`, `### Snapshot`, `### Modal state`, ...) so a model
that knows Playwright MCP needs no new habits. Large output is saved to a file in the output directory and the
response says where.
"""

import asyncio
import base64
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from markdownify import markdownify
from mcp_types import ImageContent, TextContent, Tool, ToolAnnotations

from .browser import Browser
from .session import Session, Tab, ToolError
from .snapshot import find, find_item, render

SNAPSHOT_INLINE = 12_000  # Characters of snapshot shown after an action; more is saved to a file.
SNAPSHOT_MAX = 50_000  # Characters shown by browser_snapshot.
RESULT_MAX = 40_000  # Characters of other results shown; more is saved to a file.
NEW_MESSAGES = 10  # Console messages listed under Events.


def stamp() -> str:
    return time.strftime("%Y-%m-%dT%H-%M-%S", time.gmtime()) + f"-{int(time.time() * 1000) % 1000:03d}Z"


@dataclass
class Response:
    tab: Tab | None = None
    result: list[str] = field(default_factory=list)
    images: list[ImageContent] = field(default_factory=list)
    snapshot: str | None = None  # None, "action" (sized for an action's response) or "full".
    tabs: bool = False
    page: bool = True
    after: str | None = None  # A section shown after the page's, such as an explicit snapshot.


class Tools:
    def __init__(self, session: Session, browser: Browser):
        self.session, self.browser = session, browser
        self.specs: dict[str, tuple[Tool, Callable[[dict], Awaitable[Response]], bool]] = {}
        for name, description, schema, handler, read_only, modal_ok in TOOLS:
            tool = Tool(name=name, description=description, input_schema=schema,
                        annotations=ToolAnnotations(title=name.removeprefix("browser_").replace("_", " ").capitalize(),
                                                    read_only_hint=read_only, destructive_hint=False, open_world_hint=True))
            self.specs[name] = (tool, getattr(self, handler), modal_ok)

    def definitions(self) -> list[Tool]:
        return [spec[0] for spec in self.specs.values()]

    async def call(self, name: str, args: dict) -> tuple[list, bool]:
        if name not in self.specs:
            return [TextContent(type="text", text=f"### Error\nUnknown tool {name}")], True
        _, handler, modal_ok = self.specs[name]
        tab = None
        try:
            if name != "browse" and not modal_ok and self.session.current and self.session.cdp and self.session.cdp.alive:
                tab = self.session.current
                if tab.dialog or tab.chooser:
                    raise ToolError(f'Tool "{name}" does not handle the modal state.')
            response = await handler(args or {})
            return await self.render(response), False
        except ToolError as error:
            text = f"### Error\n{error}"
            if tab and (modal := self.modal(tab)):
                text += "\n" + modal
            return [TextContent(type="text", text=text)], True
        except TimeoutError as error:
            return [TextContent(type="text", text=f"### Error\nTimed out: {error}")], True
        except Exception as error:  # Anything else is reported to the model rather than crashing the server.
            return [TextContent(type="text", text=f"### Error\n{type(error).__name__}: {error}")], True

    # Rendering.

    def modal(self, tab: Tab) -> str:
        states = []
        if tab.dialog:
            states.append(f'- ["{tab.dialog["type"]}" dialog with message "{tab.dialog["message"]}"]: can be handled by browser_handle_dialog')
        if tab.chooser:
            states.append("- [File chooser]: can be handled by browser_file_upload")
        return "### Modal state\n" + "\n".join(states) if states else ""

    def save(self, text: str | bytes, name: str) -> str:
        path = self.session.output_path(name)
        with open(path, "wb" if isinstance(text, bytes) else "w") as file:
            file.write(text)
        return path

    def fit(self, text: str, limit: int, stem: str, extension: str = "txt") -> str:
        if len(text) <= limit:
            return text
        path = self.save(text, f"{stem}-{stamp()}.{extension}")
        cut = text.rfind("\n", 0, limit)
        return text[:cut if cut > limit // 2 else limit] + f"\n… (truncated: {len(text):,} characters in all; the full output is in {path})"

    async def render(self, response: Response) -> list:
        tab = response.tab
        sections: list[str] = []
        if response.result:
            sections.append("### Result\n" + "\n".join(response.result))
        if tab and not tab.closed:
            await tab.collect()
        if tab and not tab.closed and response.page:
            title = await tab.title()
            page = [f"- Page URL: {tab.url}", f"- Page Title: {title}"]
            if tab.status and tab.status >= 400:
                page.append(f"- HTTP status: {tab.status}")
            messages = tab.console("debug", all=False)
            errors = sum(m.level == "error" for m in messages)
            warnings = sum(m.level == "warning" for m in messages)
            if errors or warnings:
                page.append(f"- Console: {errors} errors, {warnings} warnings")
            sections.append("### Page\n" + "\n".join(page))
        if response.tabs or (tab and len(self.session.tabs) > 1 and response.page):
            sections.append("### Open tabs\n" + await self.tab_list())
        if tab and not tab.closed and (modal := self.modal(tab)):
            sections.append(modal)
        elif response.after:
            sections.append(response.after)
        elif tab and not tab.closed and response.snapshot:
            sections.append(await self.snapshot_section(tab, response.snapshot))
        if tab and not tab.closed:
            events = list(tab.events)
            tab.events.clear()
            fresh = tab.new_messages()
            if fresh:
                shown = [f"- {m.line()}" for m in fresh[-NEW_MESSAGES:]]
                if len(fresh) > NEW_MESSAGES:
                    shown.insert(0, f"- {len(fresh) - NEW_MESSAGES} earlier new console messages (see browser_console_messages)")
                events.append("New console messages:\n" + "\n".join(shown))
            if events:
                sections.append("### Events\n" + "\n".join(e if e.startswith("New console") else f"- {e}" for e in events))
        text = "\n".join(sections) or "Done"
        content: list = [TextContent(type="text", text=text + "\n" if response.images else text)]
        return content + response.images

    async def snapshot_section(self, tab: Tab, mode: str) -> str:
        items = await tab.snapshot()
        text = render(items)
        limit = SNAPSHOT_INLINE if mode == "action" else SNAPSHOT_MAX
        if len(text) <= limit:
            return f"### Snapshot\n```yaml\n{text}\n```"
        path = self.save(text, f"page-{stamp()}.yml")
        if mode == "action":
            outline = [line.strip() for line in text.splitlines() if line.lstrip().startswith("- heading")][:40]
            body = f"- [Snapshot]({path}) ({len(text):,} characters, too large to show here). Search it with browser_find, " \
                   "or read parts with browser_snapshot (`target`, `depth`)."
            if outline:
                body += "\n- Headings:\n" + "\n".join(f"  {line}" for line in outline)
            return "### Snapshot\n" + body
        cut = text.rfind("\n", 0, limit)
        return f"### Snapshot\n```yaml\n{text[:cut]}\n```\n… (truncated: {len(text):,} characters in all; the full snapshot is in {path}. " \
               "Use browser_find, or `target`/`depth` to read the rest.)"

    async def tab_list(self) -> str:
        if not self.session.tabs:
            return "No open tabs. Navigate to a URL to create one."
        rows = []
        for index, tab in enumerate(self.session.tabs):
            title = await tab.title()
            context = f" (context: {tab.context})" if tab.context else ""
            current = "(current) " if tab is self.session.current else ""
            rows.append(f"- {index}: {current}[{title}]({tab.url}){context}")
        return "\n".join(rows)

    # Helpers.

    async def tab(self) -> Tab:
        return await self.session.tab()

    async def action(self, tab: Tab, work, limit: float = 2) -> Response:
        """Run a page action, then wait for what it set off, and answer with the resulting page."""
        async with tab.lock:
            navigations, downloads = tab.navigations, len(tab.downloads)
            result = await tab.act(work())
            if not (tab.dialog or tab.chooser):
                await tab.after_action(navigations, downloads, limit)
        response = Response(tab, snapshot="action")
        if isinstance(result, list):
            response.result = result
        return response

    def output(self, text: str, filename: str | None, stem: str, extension: str = "txt") -> list[str]:
        if filename:
            return [f"- [Saved to]({self.save(text, filename)})"]
        return [self.fit(text, RESULT_MAX, stem, extension)]

    # Tools.

    async def browse(self, args):
        page = await self.browser.get(args["url"])
        html = page.html
        text = html if args.get("format") == "html" else markdownify(html, heading_style="ATX")
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return Response(result=[self.fit(f"{page.status} {page.url}\n\n{text}", RESULT_MAX * 2, "browse",
                                         "html" if args.get("format") == "html" else "md")], page=False)

    async def browser_navigate(self, args):
        tab = await self.tab()
        async with tab.lock:
            await tab.navigate(args["url"])
        return Response(tab, snapshot="action")

    async def browser_navigate_back(self, args):
        tab = await self.tab()
        async with tab.lock:
            await tab.back()
        return Response(tab, snapshot="action")

    async def browser_snapshot(self, args):
        tab = await self.tab()
        async with tab.lock:
            items = await tab.snapshot(boxes=bool(args.get("boxes")))
            if target := args.get("target"):
                element = await tab.resolve(target)
                ref = next((ref for ref, el in tab.refs.items() if el.backend == element.backend and el.session == element.session), None)
                item = find_item(items, ref) if ref else None
                if item is None:
                    raise ToolError(f"{target} is not in the accessibility tree (it may be hidden or purely decorative)")
                items = [item]
            text = render(items, args.get("depth"), bool(args.get("boxes")))
        if args.get("filename"):
            return Response(tab, result=[f"- [Snapshot]({self.save(text, args['filename'])})"])
        note = ""
        if len(text) > SNAPSHOT_MAX:
            path = self.save(text, f"page-{stamp()}.yml")
            text = text[:text.rfind("\n", 0, SNAPSHOT_MAX)]
            note = f"\n… (truncated: the full snapshot is in {path}. Use browser_find, or `target`/`depth`, to read the rest.)"
        return Response(tab, after=f"### Snapshot\n```yaml\n{text}\n```{note}")

    async def browser_find(self, args):
        text, regex = args.get("text"), args.get("regex")
        if bool(text) == bool(regex):
            raise ToolError("Provide either text or regex")
        if text:
            pattern = re.compile(re.escape(text), re.I)
        else:
            match = re.fullmatch(r"/(.*)/([a-z]*)", regex, re.S)
            source, flags = (match[1], match[2]) if match else (regex, "")
            try:
                pattern = re.compile(source, (re.I if "i" in flags else 0) | (re.M if "m" in flags else 0) | (re.S if "s" in flags else 0))
            except re.error as error:
                raise ToolError(f"Invalid regex: {error}") from None
        tab = await self.tab()
        async with tab.lock:
            items = await tab.snapshot()
        count, blocks = find(items, pattern, args.get("maxResults"))
        if not count:
            return Response(tab, result=[f'No matches for {text or regex} in the page snapshot.'])
        shown = blocks.count("\n----\n") + 1
        header = f"Found {count} matches for {json.dumps(text or regex)}" + (f" (showing first {shown})" if shown < count else "") + ":"
        if args.get("filename"):
            return Response(tab, result=[header, f"- [Saved to]({self.save(blocks, args['filename'])})"])
        return Response(tab, result=[header, "", self.fit(blocks, RESULT_MAX, "find", "yml")])

    async def browser_click(self, args):
        tab = await self.tab()
        element = await tab.resolve(args["target"])
        return await self.action(tab, lambda: tab.click(element, args.get("button", "left"), 2 if args.get("doubleClick") else 1,
                                                        args.get("modifiers") or []))

    async def browser_type(self, args):
        tab = await self.tab()
        element = await tab.resolve(args["target"])
        return await self.action(tab, lambda: tab.type(element, args["text"], bool(args.get("slowly")), bool(args.get("submit"))))

    async def browser_fill_form(self, args):
        tab = await self.tab()
        fields = args.get("fields") or []
        elements = [await tab.resolve(f["target"]) for f in fields]

        async def fill():
            done = []
            for spec, element in zip(fields, elements):
                kind, value = spec.get("type", "textbox"), str(spec.get("value", ""))
                if kind == "textbox":
                    await tab.type(element, value)
                elif kind in ("checkbox", "radio"):
                    await tab.set_checked(element, value.lower() in ("true", "1", "yes", "on", "checked"))
                elif kind == "combobox":
                    await tab.select(element, [value])
                elif kind == "slider":
                    await tab.set_value(element, value)
                else:
                    raise ToolError(f"Unknown field type {kind!r}")
                done.append(f"- Filled {spec.get('name', spec['target'])}")
            return done

        return await self.action(tab, fill)

    async def browser_select_option(self, args):
        tab = await self.tab()
        element = await tab.resolve(args["target"])

        async def choose():
            return [f"Selected: {', '.join(await tab.select(element, args['values']))}"]

        return await self.action(tab, choose)

    async def browser_hover(self, args):
        tab = await self.tab()
        element = await tab.resolve(args["target"])
        return await self.action(tab, lambda: tab.hover(element), limit=1)

    async def browser_drag(self, args):
        tab = await self.tab()
        start, end = await tab.resolve(args["startTarget"]), await tab.resolve(args["endTarget"])
        return await self.action(tab, lambda: tab.drag(start, end))

    async def browser_drop(self, args):
        if not args.get("paths") and not args.get("data"):
            raise ToolError('At least one of "paths" or "data" must be provided')
        tab = await self.tab()
        element = await tab.resolve(args["target"])
        files = self.session.stage(args.get("paths") or [])
        return await self.action(tab, lambda: tab.drop(element, files, args.get("data") or {}))

    async def browser_mouse_click_xy(self, args):
        tab = await self.tab()
        x, y = float(args["x"]), float(args["y"])
        return await self.action(tab, lambda: tab.hold(args.get("modifiers") or [], lambda mask: tab.click_at(
            x, y, args.get("button", "left"), 2 if args.get("doubleClick") else 1, mask)))

    async def browser_mouse_move_xy(self, args):
        tab = await self.tab()
        return await self.action(tab, lambda: tab.mouse_to(float(args["x"]), float(args["y"])), limit=1)

    async def browser_mouse_drag_xy(self, args):
        tab = await self.tab()
        return await self.action(tab, lambda: tab.drag_xy(float(args["startX"]), float(args["startY"]), float(args["endX"]), float(args["endY"])))

    async def browser_mouse_wheel(self, args):
        tab = await self.tab()
        return await self.action(tab, lambda: tab.wheel(float(args.get("deltaX") or 0), float(args.get("deltaY") or 0)), limit=1.5)

    async def browser_press_key(self, args):
        tab = await self.tab()
        return await self.action(tab, lambda: tab.press(args["key"]))

    async def browser_wait_for(self, args):
        tab = await self.tab()
        text, gone, seconds = args.get("text"), args.get("textGone"), args.get("time")
        if not (text or gone or seconds is not None):
            raise ToolError("Either time, text or textGone must be provided")
        if seconds is not None and not (text or gone):
            await asyncio.sleep(min(float(seconds), 30))
            return Response(tab, result=[f"Waited for {seconds} seconds"], snapshot="action")
        limit = min(float(seconds), 30) if seconds is not None else 30
        deadline = time.monotonic() + limit
        checks = 0
        while True:
            async with tab.lock:
                body = await self.page_text(tab, deep=checks % 4 == 3)
            checks += 1
            if (not text or text in body) and (not gone or gone not in body):
                break
            if time.monotonic() > deadline:
                raise ToolError(f"Timed out after {limit:.0f}s waiting for " + (f'text "{text}" to appear' if text else f'text "{gone}" to disappear'))
            await asyncio.sleep(0.25)
        return Response(tab, result=[f'Waited for {"text " + json.dumps(text) if text else "text " + json.dumps(gone) + " to disappear"}'], snapshot="action")

    async def page_text(self, tab: Tab, deep: bool) -> str:
        """The page's visible text; `deep` also reads shadow DOM and iframes through the accessibility tree."""
        try:
            text = await tab.eval("document.body ? document.body.innerText : ''", isolated=True) or ""
        except Exception:
            text = ""
        if deep:
            try:
                text += "\n" + render(await tab.snapshot())
            except Exception:
                pass
        return text

    async def browser_take_screenshot(self, args):
        tab = await self.tab()
        kind = args.get("type") or "png"
        target = args.get("target")
        if target and args.get("fullPage"):
            raise ToolError("fullPage cannot be used with an element screenshot")
        async with tab.lock:
            element = await tab.resolve(target) if target else None
            full, small, (width, height) = await tab.screenshot(kind, bool(args.get("fullPage")), element)
        extension = "jpg" if kind == "jpeg" else kind
        path = self.save(full, args.get("filename") or f"page-{stamp()}.{extension}")
        what = f"element {args.get('element') or target}" if target else "full page" if args.get("fullPage") else "viewport"
        result = [f"- [Screenshot of {what}]({path}) ({width}x{height} CSS pixels)"]
        if small is not full:
            result.append("- The image below is scaled down to fit; the file has full resolution.")
        response = Response(tab, result=result)
        response.images = [ImageContent(type="image", data=base64.b64encode(small).decode(), mime_type=f"image/{kind}")]
        return response

    async def browser_evaluate(self, args):
        tab = await self.tab()
        async with tab.lock:
            element = await tab.resolve(args["target"]) if args.get("target") else None
            value = await tab.act(tab.evaluate(args["function"], element))
        if tab.dialog:
            return Response(tab, result=["The script opened a dialog before returning."])
        text = "undefined" if value is None else json.dumps(value, indent=2, ensure_ascii=False, default=str)
        return Response(tab, result=self.output(text, args.get("filename"), "evaluate", "json"))

    async def browser_tabs(self, args):
        action = args.get("action")
        session = self.session
        if action == "list":
            await session.ensure()
            return Response(session.current, tabs=True, page=False)
        if action == "new":
            tab = await session.new_tab(context=args.get("isolatedContext") if args.get("isolatedContext") not in (None, "", "default") else None)
            if url := args.get("url"):
                async with tab.lock:
                    await tab.navigate(url)
            return Response(tab, tabs=True, snapshot="action")
        if action in ("close", "select"):
            if not session.tabs:
                raise ToolError("No open tabs")
            index = args.get("index")
            if index is None:
                if action == "select":
                    raise ToolError("Tab index is required to select a tab")
                tab = session.current or session.tabs[-1]
            elif not 0 <= int(index) < len(session.tabs):
                raise ToolError(f"Tab {index} not found; there are {len(session.tabs)} tabs")
            else:
                tab = session.tabs[int(index)]
            if action == "close":
                await session.close_tab(tab)
                return Response(session.current, tabs=True, snapshot="action" if session.current else None)
            await session.select(tab)
            return Response(tab, tabs=True, snapshot="action")
        raise ToolError(f"Unknown action {action!r}")

    async def browser_handle_dialog(self, args):
        tab = await self.tab()
        navigations, downloads = tab.navigations, len(tab.downloads)
        await tab.handle_dialog(bool(args.get("accept")), args.get("promptText"))
        await tab.after_action(navigations, downloads)
        return Response(tab, snapshot="action")

    async def browser_file_upload(self, args):
        tab = await self.tab()
        navigations, downloads = tab.navigations, len(tab.downloads)
        await tab.upload(args.get("paths"))
        await tab.after_action(navigations, downloads)
        return Response(tab, snapshot="action")

    async def browser_console_messages(self, args):
        tab = await self.tab()
        await tab.collect()
        level = args.get("level") or "info"
        messages = tab.console(level, bool(args.get("all")))
        everything = tab.console("debug", bool(args.get("all")))
        header = f"Total messages: {len(everything)} (Errors: {sum(m.level == 'error' for m in everything)}, " \
                 f"Warnings: {sum(m.level == 'warning' for m in everything)})"
        if tab.browser.config.console != "full" and not tab.runtime:
            header += "\nCapturing page errors, uncaught rejections and browser messages only: console.log calls are not " \
                      "recorded on public sites, because the capture they need can be detected by bot walls."
        text = "\n".join(m.line() for m in messages) or "(no messages)"
        tab.console_reported = len(tab.messages)
        return Response(tab, result=[header, ""] + self.output(text, args.get("filename"), "console", "log"), page=False)

    async def browser_network_requests(self, args):
        tab = await self.tab()
        try:
            requests = tab.network(bool(args.get("static")), bool(args.get("all")), args.get("filter"))
        except re.error as error:
            raise ToolError(f"Invalid filter: {error}") from None
        text = "\n".join(r.line() for r in requests) or "(no requests)"
        return Response(tab, result=self.output(text, args.get("filename"), "network"), page=False)

    async def browser_network_request(self, args):
        tab = await self.tab()
        text, binary = await tab.request_detail(int(args["index"]), args.get("part"))
        if binary is not None and (args.get("filename") or args.get("part") == "response-body"):
            path = self.save(binary, args.get("filename") or f"response-{args['index']}-{stamp()}.bin")
            return Response(tab, result=[text, f"- [Saved to]({path})"], page=False)
        return Response(tab, result=self.output(text, args.get("filename"), f"request-{args['index']}"), page=False)

    async def browser_resize(self, args):
        tab = await self.tab()
        async with tab.lock:
            await tab.resize(int(args["width"]), int(args["height"]))
            await tab.quiet(1)
        return Response(tab, result=[f"Resized the viewport to {args['width']}x{args['height']}"], snapshot="action")

    async def browser_close(self, args):
        await self.session.close_all()
        return Response(result=["Closed all tabs. Cookies and site clearances are kept for the next page."], page=False)

    async def browser_emulate_media(self, args):
        tab = await self.tab()
        settings = {key: args[key] for key in ("colorScheme", "contrast", "forcedColors", "media", "reducedMotion") if key in args}
        await tab.emulate_media(**settings)
        shown = ", ".join(f"{k}={v}" for k, v in tab.media.items()) or "none"
        return Response(tab, result=[f"Emulated media: {shown}"])


TARGET = {"type": "string", "description": "Exact target element reference from the page snapshot, or a unique element selector"}
ELEMENT = {"type": "string", "description": "Human-readable element description used to obtain permission to interact with the element"}
FILENAME = lambda what: {"type": "string", "description": f"File name to save {what} to. Relative file names are resolved against the output directory."}


def obj(properties: dict, required: list[str] = ()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


# (name, description, input schema, handler, read-only, allowed while a dialog or file chooser is open)
TOOLS = [
    ("browse", "Fetch a web page like a real Chrome, passing Anubis and Cloudflare challenges, and return its text as markdown "
     "(or raw HTML). Fast for reading: it renders JavaScript only when the page needs it, and keeps no tab open. Use "
     "browser_navigate to interact with a page.",
     obj({"url": {"type": "string", "description": "The URL to fetch"},
          "format": {"type": "string", "enum": ["markdown", "html"], "default": "markdown", "description": "Output format"}}, ["url"]),
     "browse", True, True),
    ("browser_navigate", "Navigate to a URL. Bot walls (Cloudflare, Turnstile, Anubis and similar) are passed automatically.",
     obj({"url": {"type": "string", "description": "The URL to navigate to"}}, ["url"]), "browser_navigate", False, False),
    ("browser_navigate_back", "Go back to the previous page in the history", obj({}), "browser_navigate_back", False, False),
    ("browser_snapshot", "Capture accessibility snapshot of the current page, this is better than screenshot",
     obj({"target": TARGET | {"description": "Exact target element reference from the page snapshot, or a unique element selector; snapshots only that element's subtree"},
          "depth": {"type": "number", "description": "Limit the depth of the snapshot tree"},
          "boxes": {"type": "boolean", "description": "Include each element's bounding box as [box=x,y,width,height] in the snapshot. Coordinates are viewport-relative, in CSS pixels"},
          "filename": FILENAME("the snapshot")}), "browser_snapshot", True, False),
    ("browser_find", "Search the accessibility snapshot of the current page for text or a regular expression. Returns matching snapshot "
     "nodes with a few lines of surrounding context, each shown under its path from the root of the tree, which is cheaper "
     "than capturing the whole snapshot when you only need to locate an element and its ref.",
     obj({"text": {"type": "string", "description": "Plain text to search for in the page snapshot (case-insensitive substring match). Provide either text or regex, not both."},
          "regex": {"type": "string", "description": "Regular expression to search for in the page snapshot. Matching is case-sensitive by default; wrap the pattern in slashes to add flags, e.g. \"/error/i\" for case-insensitive. Provide either text or regex, not both."},
          "maxResults": {"type": "number", "description": "Maximum number of matching items to return. Defaults to returning all matches."},
          "filename": FILENAME("the results")}), "browser_find", True, False),
    ("browser_click", "Perform click on a web page",
     obj({"element": ELEMENT, "target": TARGET,
          "doubleClick": {"type": "boolean", "description": "Whether to perform a double click instead of a single click"},
          "button": {"type": "string", "enum": ["left", "right", "middle"], "description": "Button to click, defaults to left"},
          "modifiers": {"type": "array", "items": {"type": "string", "enum": ["Alt", "Control", "ControlOrMeta", "Meta", "Shift"]}, "description": "Modifier keys to press"}},
         ["target"]), "browser_click", False, False),
    ("browser_type", "Type text into editable element",
     obj({"element": ELEMENT, "target": TARGET, "text": {"type": "string", "description": "Text to type into the element"},
          "submit": {"type": "boolean", "description": "Whether to submit entered text (press Enter after)"},
          "slowly": {"type": "boolean", "description": "Whether to type one character at a time. Useful for triggering key handlers in the page. By default entire text is filled in at once."}},
         ["target", "text"]), "browser_type", False, False),
    ("browser_fill_form", "Fill multiple form fields",
     obj({"fields": {"type": "array", "description": "Fields to fill in", "items": obj({
         "element": ELEMENT, "target": TARGET, "name": {"type": "string", "description": "Human-readable field name"},
         "type": {"type": "string", "enum": ["textbox", "checkbox", "radio", "combobox", "slider"], "description": "Type of the field"},
         "value": {"type": "string", "description": "Value to fill in the field. If the field is a checkbox, the value should be `true` or `false`. If the field is a combobox, the value should be the text of the option."}},
         ["target", "name", "type", "value"])}}, ["fields"]), "browser_fill_form", False, False),
    ("browser_select_option", "Select an option in a dropdown",
     obj({"element": ELEMENT, "target": TARGET,
          "values": {"type": "array", "items": {"type": "string"}, "description": "Array of values to select in the dropdown. This can be a single value or multiple values."}},
         ["target", "values"]), "browser_select_option", False, False),
    ("browser_hover", "Hover over element on page", obj({"element": ELEMENT, "target": TARGET}, ["target"]), "browser_hover", False, False),
    ("browser_drag", "Perform drag and drop between two elements",
     obj({"startElement": ELEMENT | {"description": "Human-readable source element description used to obtain the permission to interact with the element"},
          "startTarget": TARGET, "endElement": ELEMENT | {"description": "Human-readable target element description used to obtain the permission to interact with the element"},
          "endTarget": TARGET}, ["startTarget", "endTarget"]), "browser_drag", False, False),
    ("browser_drop", "Drop files or MIME-typed data onto an element, as if dragged from outside the page. At least one of \"paths\" or \"data\" must be provided.",
     obj({"element": ELEMENT, "target": TARGET,
          "paths": {"type": "array", "items": {"type": "string"}, "description": "Absolute paths to files to drop onto the element."},
          "data": {"type": "object", "additionalProperties": {"type": "string"}, "description": "Data to drop, as a map of MIME type to string value (e.g. {\"text/plain\": \"hello\", \"text/uri-list\": \"https://example.com\"})."}},
         ["target"]), "browser_drop", False, False),
    ("browser_press_key", "Press a key on the keyboard",
     obj({"key": {"type": "string", "description": "Name of the key to press or a character to generate, such as `ArrowLeft` or `a`. Combinations like `Control+a` work too."}}, ["key"]),
     "browser_press_key", False, False),
    ("browser_mouse_click_xy", "Click at a position in the viewport, in CSS pixels, as seen in a screenshot. Prefer browser_click with a ref; use this for canvases, maps and image grids.",
     obj({"element": ELEMENT, "x": {"type": "number", "description": "X coordinate"}, "y": {"type": "number", "description": "Y coordinate"},
          "button": {"type": "string", "enum": ["left", "right", "middle"], "description": "Button to click, defaults to left"},
          "doubleClick": {"type": "boolean", "description": "Whether to perform a double click instead of a single click"},
          "modifiers": {"type": "array", "items": {"type": "string", "enum": ["Alt", "Control", "ControlOrMeta", "Meta", "Shift"]}, "description": "Modifier keys to press"}},
         ["x", "y"]), "browser_mouse_click_xy", False, False),
    ("browser_mouse_move_xy", "Move the mouse to a position in the viewport, in CSS pixels",
     obj({"element": ELEMENT, "x": {"type": "number", "description": "X coordinate"}, "y": {"type": "number", "description": "Y coordinate"}}, ["x", "y"]),
     "browser_mouse_move_xy", False, False),
    ("browser_mouse_drag_xy", "Drag the left mouse button from one position in the viewport to another, in CSS pixels",
     obj({"element": ELEMENT, "startX": {"type": "number", "description": "Start X coordinate"}, "startY": {"type": "number", "description": "Start Y coordinate"},
          "endX": {"type": "number", "description": "End X coordinate"}, "endY": {"type": "number", "description": "End Y coordinate"}},
         ["startX", "startY", "endX", "endY"]), "browser_mouse_drag_xy", False, False),
    ("browser_mouse_wheel", "Scroll the mouse wheel at the mouse position, for example to load more of an infinite list",
     obj({"deltaX": {"type": "number", "description": "Pixels to scroll horizontally"}, "deltaY": {"type": "number", "description": "Pixels to scroll vertically; positive scrolls down"}}),
     "browser_mouse_wheel", False, False),
    ("browser_wait_for", "Wait for text to appear or disappear or a specified time to pass. When both text and textGone are provided, waits for the first one to happen",
     obj({"time": {"type": "number", "description": "The time to wait in seconds, at most 30. When combined with text or textGone, serves as a timeout for them instead of the default action timeout"},
          "text": {"type": "string", "description": "The text to wait for"},
          "textGone": {"type": "string", "description": "The text to wait for to disappear"}}), "browser_wait_for", True, False),
    ("browser_take_screenshot", "Take a screenshot of the current page. You can't perform actions based on the screenshot, use browser_snapshot for actions.",
     obj({"type": {"type": "string", "enum": ["png", "jpeg", "webp"], "description": "Image format for the screenshot. Default is png."},
          "filename": FILENAME("the screenshot") | {"description": "File name to save the screenshot to. Relative file names are resolved against the output directory. If not specified, the screenshot is saved as `page-{timestamp}.{png|jpeg|webp}`."},
          "element": ELEMENT, "target": TARGET,
          "fullPage": {"type": "boolean", "description": "When true, takes a screenshot of the full scrollable page, instead of the currently visible viewport. Cannot be used with element screenshots."},
          "scale": {"type": "string", "enum": ["css", "device"], "default": "css", "description": "Image resolution scale. \"css\" produces a screenshot sized in CSS pixels. \"device\" produces a high-resolution screenshot using device pixels."}}),
     "browser_take_screenshot", True, False),
    ("browser_evaluate", "Evaluate JavaScript expression on page or element",
     obj({"function": {"type": "string", "description": "() => { /* code */ } or (element) => { /* code */ } when element is provided"},
          "element": ELEMENT, "target": TARGET, "filename": FILENAME("the result")}, ["function"]), "browser_evaluate", False, False),
    ("browser_tabs", "List, create, close, or select a browser tab.",
     obj({"action": {"type": "string", "enum": ["list", "new", "close", "select"], "description": "Operation to perform"},
          "index": {"type": "number", "description": "Tab index, used for close/select. If omitted for close, current tab is closed."},
          "url": {"type": "string", "description": "URL to navigate to in the new tab, used for new."},
          "isolatedContext": {"type": "string", "description": "Name of an isolated browser context to create the new tab in, used for new. Tabs in the same isolated context share cookies and storage, tabs in different contexts are fully isolated. If omitted or \"default\", the tab is created in the default context."}},
         ["action"]), "browser_tabs", False, True),
    ("browser_handle_dialog", "Handle a dialog",
     obj({"accept": {"type": "boolean", "description": "Whether to accept the dialog."},
          "promptText": {"type": "string", "description": "The text of the prompt in case of a prompt dialog."}}, ["accept"]),
     "browser_handle_dialog", False, True),
    ("browser_file_upload", "Upload one or multiple files",
     obj({"paths": {"type": "array", "items": {"type": "string"}, "description": "The absolute paths to the files to upload. Can be single file or multiple files. If omitted, file chooser is cancelled."}}),
     "browser_file_upload", False, True),
    ("browser_console_messages", "Returns all console messages",
     obj({"level": {"type": "string", "enum": ["error", "warning", "info", "debug"], "default": "info", "description": "Level of the console messages to return. Each level includes the messages of more severe levels. Defaults to \"info\"."},
          "all": {"type": "boolean", "description": "Return all console messages since the beginning of the session, not just since the last navigation. Defaults to false."},
          "filename": FILENAME("the console messages")}), "browser_console_messages", True, True),
    ("browser_network_requests", "Returns a numbered list of network requests since loading the page. Use browser_network_request with the number to get full details.",
     obj({"static": {"type": "boolean", "default": False, "description": "Whether to include successful static resources like images, fonts, scripts, etc. Defaults to false."},
          "all": {"type": "boolean", "description": "Return all network requests since the beginning of the session, not just since the last navigation. Defaults to false."},
          "filter": {"type": "string", "description": "Only return requests whose URL matches this regexp (e.g. \"/api/.*user\")."},
          "filename": FILENAME("the network requests")}), "browser_network_requests", True, True),
    ("browser_network_request", "Returns full details (headers and body) of a single network request, or a single part if `part` is set. Use the number from browser_network_requests.",
     obj({"index": {"type": "integer", "minimum": 1, "description": "1-based index of the request, as printed by browser_network_requests."},
          "part": {"type": "string", "enum": ["request-headers", "request-body", "response-headers", "response-body"], "description": "Return only this part of the request. Omit to return full details."},
          "filename": FILENAME("the result")}, ["index"]), "browser_network_request", True, True),
    ("browser_resize", "Resize the browser window",
     obj({"width": {"type": "number", "description": "Width of the browser window"}, "height": {"type": "number", "description": "Height of the browser window"}},
         ["width", "height"]), "browser_resize", False, False),
    ("browser_close", "Close the page", obj({}), "browser_close", False, True),
    ("browser_emulate_media", "Emulate CSS media features for the page, for example switch between the light and dark color scheme. Omitted parameters are left unchanged; null clears an override.",
     obj({"colorScheme": {"anyOf": [{"type": "string", "enum": ["light", "dark"]}, {"type": "null"}], "description": "Emulates the prefers-color-scheme media feature"},
          "contrast": {"anyOf": [{"type": "string", "enum": ["more", "no-preference"]}, {"type": "null"}], "description": "Emulates the prefers-contrast media feature"},
          "forcedColors": {"anyOf": [{"type": "string", "enum": ["active", "none"]}, {"type": "null"}], "description": "Emulates the forced-colors media feature"},
          "media": {"anyOf": [{"type": "string", "enum": ["screen", "print"]}, {"type": "null"}], "description": "Changes the CSS media type of the page"},
          "reducedMotion": {"anyOf": [{"type": "string", "enum": ["reduce", "no-preference"]}, {"type": "null"}], "description": "Emulates the prefers-reduced-motion media feature"}}),
     "browser_emulate_media", False, False),
]
