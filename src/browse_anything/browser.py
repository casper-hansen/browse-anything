import asyncio
import re
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit

import wreq

from . import anubis, cloudflare
from .session import Session


@dataclass
class Page:
    url: str
    status: int
    html: str


def is_shell(html: str) -> bool:
    """Whether a page is an empty shell that JavaScript fills in, such as a single-page app's."""
    body = re.search(r"<body[^>]*>(.*)</body>", html, re.S | re.I)
    text = re.sub(r"<script\b.*?</script>|<style\b.*?</style>|<noscript\b.*?</noscript>|<[^>]+>|\s+", " ", body[1] if body else html, flags=re.S | re.I)
    if len(text.split()) >= 30 or len(text) >= 300:
        return False
    # Little text is only a shell if something says an app fills it in, not merely a small page with a script.
    mount = re.search(r"""<div[^>]+id=["']?(root|app|__next|__nuxt|svelte|main|application)["']?[^>]*>\s*</div>""", html, re.I)
    bundles = len(re.findall(r"<script[^>]+src=", html, re.I))
    return bool(mount or re.search(r"<noscript\b[^>]*>.*?javascript", html, re.S | re.I) or bundles >= 3)


class Browser:
    """wreq with a Chrome fingerprint; challenges are solved once per site and their cookies reused.

    Pages behind Cloudflare, and pages that are empty without JavaScript, are rendered in the session's Chrome.
    """

    def __init__(self, session: Session | None = None):
        self.jar = wreq.Jar()
        self.client = wreq.Client(
            emulation=wreq.Emulation.Chrome154,
            cookie_provider=self.jar,
            redirect=wreq.redirect.Policy.limited(10),
            timeout=timedelta(seconds=30),
        )
        self.session = session or Session()
        self.owns_session = session is None
        self.user_agents: dict[str, str] = {}  # cf_clearance is bound to the user agent that earned it.

    async def get(self, url: str, render: bool | None = None) -> Page:
        """`render`: True always uses Chrome, False never does, None (default) when the page needs it."""
        if render:
            return await self.render(url)
        page, headers = await self.fetch(url)
        if anubis.detect(page.html):
            refresh = headers.get("refresh")
            redeem, delay = anubis.solve(page.url, page.html, refresh and bytes(refresh).decode())
            await asyncio.sleep(delay)
            page, headers = await self.fetch(redeem)
        if cloudflare.detect(headers, page.html) or (render is None and is_shell(page.html)):
            page = await self.render(page.url)
        return page

    async def render(self, url: str) -> Page:
        # Chrome passes any challenge and renders the page, JavaScript included. Its cookies go to wreq, so later
        # requests to the site skip Chrome.
        final, status, html, cookies, agent = await self.session.render(url)
        self.user_agents[urlsplit(final).hostname] = agent
        for cookie in cookies:
            self.jar.add(f"{cookie['name']}={cookie['value']}; Domain={cookie['domain']}; Path={cookie['path']}", final)
        return Page(final, status, html)

    async def fetch(self, url: str) -> tuple[Page, wreq.HeaderMap]:
        user_agent = self.user_agents.get(urlsplit(url).hostname)
        async with await self.client.get(url, headers={"User-Agent": user_agent} if user_agent else {}) as response:
            return Page(response.url, response.status.as_int(), await response.text()), response.headers

    def close(self):
        if self.owns_session:
            self.session.close()
