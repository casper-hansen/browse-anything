import argparse
import asyncio
import os
import signal
from typing import Literal

from markdownify import markdownify
from mcp.server.mcpserver import MCPServer

from .browser import Browser


def render(html: str, format: Literal["markdown", "html"]) -> str:
    return html if format == "html" else markdownify(html, heading_style="ATX")


def serve():
    browser = Browser()

    # MCP clients stop a stdio server by closing stdin, then SIGTERM, then SIGKILL. On SIGTERM, close Chrome and remove
    # its profile, then exit at once: a normal exit would wait on the thread blocked reading stdin.
    def stop(*_):
        browser.close()
        os._exit(0)

    signal.signal(signal.SIGTERM, stop)
    server = MCPServer("browse-anything")

    @server.tool()
    async def browse(url: str, format: Literal["markdown", "html"] = "markdown") -> str:
        """Fetch a web page like a real Chrome, passing Anubis and Cloudflare challenges. Returns markdown or raw HTML."""
        page = await browser.get(url)
        return f"{page.status} {page.url}\n\n{render(page.html, format)}"

    try:
        server.run("stdio")
    finally:
        browser.close()


async def fetch(url: str, format: Literal["markdown", "html"]):
    browser = Browser()
    try:
        page = await browser.get(url)
        print(render(page.html, format))
    finally:
        browser.close()


def main():
    parser = argparse.ArgumentParser(prog="browse-anything", description="Print a page, or run as an MCP server without a URL.")
    parser.add_argument("url", nargs="?")
    parser.add_argument("--html", action="store_true", help="print raw HTML instead of markdown")
    args = parser.parse_args()
    if args.url:
        asyncio.run(fetch(args.url, "html" if args.html else "markdown"))
    else:
        serve()


if __name__ == "__main__":
    main()
