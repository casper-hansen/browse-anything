import asyncio
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit

import wreq

from . import anubis, cloudflare


@dataclass
class Page:
    url: str
    status: int
    html: str


class Browser:
    """wreq with a Chrome fingerprint; challenges are solved once per site and their cookies reused."""

    def __init__(self):
        self.jar = wreq.Jar()
        self.client = wreq.Client(
            emulation=wreq.Emulation.Chrome154,
            cookie_provider=self.jar,
            redirect=wreq.redirect.Policy.limited(10),
            timeout=timedelta(seconds=30),
        )
        self.chrome = cloudflare.Chrome()
        self.user_agents: dict[str, str] = {}  # cf_clearance is bound to the user agent that earned it.

    async def get(self, url: str) -> Page:
        page, headers = await self.fetch(url)
        if anubis.detect(page.html):
            refresh = headers.get("refresh")
            redeem, delay = anubis.solve(page.url, page.html, refresh and bytes(refresh).decode())
            await asyncio.sleep(delay)
            page, headers = await self.fetch(redeem)
        if cloudflare.detect(headers, page.html):
            # Chrome stops once it holds the clearance, so the origin, which can be slow, only serves the page once.
            if html := await self.clear(page.url, "clearance"):  # Chrome was let in without a new clearance.
                return Page(page.url, 200, html)
            retry, headers = await self.fetch(page.url)
            if cloudflare.detect(headers, retry.html):  # The clearance did not carry over to wreq; let Chrome load it.
                retry = Page(page.url, 200, await self.clear(page.url, "page"))
            page = retry
        return page

    async def clear(self, url: str, until: str) -> str | None:
        html, cookies, user_agent = await self.chrome.solve(url, until)
        self.user_agents[urlsplit(url).hostname] = user_agent
        for cookie in cookies:
            self.jar.add(f"{cookie['name']}={cookie['value']}; Domain={cookie['domain']}; Path={cookie['path']}", url)
        return html

    async def fetch(self, url: str) -> tuple[Page, wreq.HeaderMap]:
        user_agent = self.user_agents.get(urlsplit(url).hostname)
        async with await self.client.get(url, headers={"User-Agent": user_agent} if user_agent else {}) as response:
            return Page(response.url, response.status.as_int(), await response.text()), response.headers

    def close(self):
        self.chrome.close()
