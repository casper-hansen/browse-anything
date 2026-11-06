import argparse
import asyncio
import os
import signal

import anyio
from markdownify import markdownify
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp_types import CallToolResult, ListToolsResult

from .browser import Browser
from .session import Config, Session
from .tools import Tools


def config_from_env() -> Config:
    width, _, height = os.environ.get("BROWSE_ANYTHING_VIEWPORT", "1280x720").partition("x")
    console = os.environ.get("BROWSE_ANYTHING_CONSOLE", "auto")
    return Config(profile=os.environ.get("BROWSE_ANYTHING_PROFILE", ""),
                  output_dir=os.environ.get("BROWSE_ANYTHING_OUTPUT_DIR", ""),
                  console=console if console in ("auto", "stealth", "full") else "auto",
                  width=int(width or 1280), height=int(height or 720))


def serve(config: Config):
    session = Session(config)
    browser = Browser(session)
    tools = Tools(session, browser)

    # MCP clients stop a stdio server by closing stdin, then SIGTERM, then SIGKILL. On SIGTERM, close Chrome and remove
    # its profile, then exit at once: a normal exit would wait on the thread blocked reading stdin.
    def stop(*_):
        session.close()
        os._exit(0)

    signal.signal(signal.SIGTERM, stop)

    async def list_tools(ctx, params) -> ListToolsResult:
        return ListToolsResult(tools=tools.definitions())

    async def call_tool(ctx, params) -> CallToolResult:
        content, error = await tools.call(params.name, params.arguments or {})
        return CallToolResult(content=content, is_error=error)

    server = Server("browse-anything", on_list_tools=list_tools, on_call_tool=call_tool)

    async def main():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    try:
        anyio.run(main)
    finally:
        session.close()


async def fetch(url: str, html: bool, render: bool | None):
    browser = Browser(Session(config_from_env()))
    try:
        page = await browser.get(url, render)
        print(page.html if html else markdownify(page.html, heading_style="ATX"))
    finally:
        browser.session.close()


def main():
    parser = argparse.ArgumentParser(prog="browse-anything", description="Print a page, or run as an MCP server without a URL.")
    parser.add_argument("url", nargs="?")
    parser.add_argument("--html", action="store_true", help="print raw HTML instead of markdown")
    parser.add_argument("--render", action=argparse.BooleanOptionalAction, default=None,
                        help="always (--render) or never (--no-render) render in Chrome; by default only when the page needs it")
    args = parser.parse_args()
    if args.url:
        asyncio.run(fetch(args.url, args.html, args.render))
    else:
        serve(config_from_env())


if __name__ == "__main__":
    main()
