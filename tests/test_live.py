"""End-to-end tests over MCP stdio against a real, sandboxed Chrome. They need Chrome and, for Cloudflare, the internet.

Run with `BROWSE_ANYTHING_LIVE=1 uv run pytest tests/test_live.py`.
"""

import functools
import http.server
import os
import re
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

pytestmark = [pytest.mark.skipif(not os.environ.get("BROWSE_ANYTHING_LIVE"), reason="set BROWSE_ANYTHING_LIVE=1"), pytest.mark.anyio]

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("site")
    (root / "playground.html").write_text((FIXTURES / "playground.html").read_text())
    (root / "download.txt").write_text("hello download\n")
    (root / "second.html").write_text('<title>Second</title><h1>Second page</h1><a href="/playground.html">back</a>')
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@asynccontextmanager
async def browser(tmp_path):
    params = StdioServerParameters(command=sys.executable, args=["-m", "browse_anything"],
                                   env={"BROWSE_ANYTHING_OUTPUT_DIR": str(tmp_path / "out"), **{k: v for k, v in os.environ.items() if k.startswith("BROWSE_ANYTHING_CHROME")}})
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()

        async def call(name, **args):
            result = await session.call_tool(name, args)
            text = "\n".join(c.text for c in result.content if c.type == "text")
            images = [c for c in result.content if c.type == "image"]
            return text, images, result.is_error

        yield session, call


def ref(text: str, line: str) -> str:
    """The ref on the snapshot line that contains `line`."""
    return next(re.search(r"\[ref=(\w+)\]", l)[1] for l in text.splitlines() if line in l and "[ref=" in l)


async def test_playground(site, tmp_path):
    upload = tmp_path / "upload.txt"
    upload.write_text("upload me")
    async with browser(tmp_path) as (session, call):
        names = {tool.name for tool in (await session.list_tools()).tools}
        assert {"browse", "browser_navigate", "browser_snapshot", "browser_click", "browser_take_screenshot", "browser_evaluate"} <= names

        text, _, error = await call("browser_navigate", url=f"{site}/playground.html")
        assert not error and "- Page Title: Playground" in text and "### Snapshot" in text

        await call("browser_type", target=ref(text, 'textbox "Name"'), text="Casper")
        await call("browser_select_option", target=ref(text, 'combobox "Color"'), values=["Blue"])
        await call("browser_fill_form", fields=[{"target": ref(text, 'checkbox "I agree"'), "name": "agree", "type": "checkbox", "value": "true"}])
        after, _, _ = await call("browser_click", target=ref(text, 'button "Submit"'))
        assert "submitted Casper b on" in after

        after, _, _ = await call("browser_click", target=ref(text, 'button "Alert"'))
        assert '["alert" dialog with message "hello there"]' in after
        _, _, error = await call("browser_snapshot")
        assert error
        await call("browser_handle_dialog", accept=True)

        after, _, _ = await call("browser_click", target=ref(text, 'button "File"'))
        assert "[File chooser]" in after
        after, _, _ = await call("browser_file_upload", paths=[str(upload)])
        assert "file upload.txt 9" in after

        after, _, _ = await call("browser_click", target=ref(text, 'combobox "Custom select"'))  # A 0x0 input.
        assert "custom opened" in after and "enclosing control was clicked" in after
        # A popover without a role covers a button: the error leads with its close button.
        after, _, error = await call("browser_click", target=ref(text, 'button "Under popover"'))
        assert error and re.search(r'overlay whose controls are: button "Close welcome" \[ref=\w+\], link "Sign in"', after)
        await call("browser_click", target=ref(text, 'button "Close welcome"'))
        after, _, _ = await call("browser_click", target=ref(text, 'button "Under popover"'))
        assert "under clicked" in after
        after, _, _ = await call("browser_click", target=ref(text, 'button "Shadow button"'))
        assert "shadow clicked" in after
        after, _, _ = await call("browser_click", target=ref(text, 'button "Inner button"'))
        assert "inner clicked" in after
        after, _, _ = await call("browser_drag", startTarget=ref(text, "Drag me"), endTarget=ref(text, "Drop target"))
        assert "dropped dragged!" in after

        after, _, _ = await call("browser_click", target=ref(text, 'link "Download"'))
        assert re.search(r"Downloaded file hello.txt to (\S+)", after)
        assert Path(re.search(r"Downloaded file hello.txt to (\S+)", after)[1]).read_text() == "hello download\n"

        after, _, _ = await call("browser_click", target=ref(text, 'link "Open popup"'))
        assert "- 1: (current) [Popup]" in after and "now the current tab" in after  # Switched to the new tab.
        await call("browser_tabs", action="close", index=1)

        value, _, _ = await call("browser_evaluate", function="() => ({title: document.title, n: 6 * 7})")
        assert '"n": 42' in value
        shot, images, _ = await call("browser_take_screenshot")
        assert images and images[0].mime_type == "image/png" and "Screenshot of viewport" in shot

        await call("browser_click", target=ref(text, 'button "Console"'))
        messages, _, _ = await call("browser_console_messages", level="debug")
        assert "[LOG] logged {a: 1}" in messages and "boom" in messages

        await call("browser_click", target=ref(text, 'link "Second page"'))
        back, _, _ = await call("browser_navigate_back")
        assert "- Page Title: Playground" in back


async def test_cloudflare(tmp_path):
    async with browser(tmp_path) as (session, call):
        text, _, error = await call("browser_navigate", url="https://www.scrapingcourse.com/cloudflare-challenge")
        assert not error and "You bypassed the Cloudflare challenge" in text
        text, _, error = await call("browse", url="https://www.scrapingcourse.com/cloudflare-challenge")
        assert not error and "You bypassed the Cloudflare challenge" in text
