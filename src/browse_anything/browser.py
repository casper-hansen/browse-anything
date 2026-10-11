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
            html, cookies, user_agent = await self.chrome.solve(page.url)
            self.user_agents[urlsplit(page.url).hostname] = user_agent
            for cookie in cookies:
                self.jar.add(f"{cookie['name']}={cookie['value']}; Domain={cookie['domain']}; Path={cookie['path']}", page.url)
            retry, headers = await self.fetch(page.url)
            page = Page(page.url, 200, html) if cloudflare.detect(headers, retry.html) else retry
        return page

    async def fetch(self, url: str) -> tuple[Page, wreq.HeaderMap]:
        user_agent = self.user_agents.get(urlsplit(url).hostname)
        async with await self.client.get(url, headers={"User-Agent": user_agent} if user_agent else {}) as response:
            return Page(response.url, response.status.as_int(), await response.text()), response.headers

    def close(self):
        self.chrome.close()
