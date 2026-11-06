"""Find and launch Chrome: new headless mode, a regular user agent, inside DSH's sandbox."""

import glob
import os
import re
import shutil
import subprocess
import sys

from .cdp import CDP
from .sandbox import confine

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    # Playwright's own Chromium (Chrome for Testing), newest first.
    *sorted(glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome")), reverse=True),
    *sorted(glob.glob(os.path.expanduser("~/Library/Caches/ms-playwright/chromium-*/chrome-mac*/*.app/Contents/MacOS/*")), reverse=True),
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
    "--headless=new",
    "--mute-audio",
]

PLATFORMS = {"darwin": "Macintosh; Intel Mac OS X 10_15_7", "linux": "X11; Linux x86_64"}


def find_chrome() -> str:
    for candidate in [os.environ.get("BROWSE_ANYTHING_CHROME"), *CHROME_CANDIDATES]:
        if candidate and (path := shutil.which(candidate) or (os.path.isfile(candidate) and candidate)):
            return path
    raise RuntimeError("Chrome not found; install Chrome or Chromium, or set BROWSE_ANYTHING_CHROME")


def user_agent(chrome: str) -> str:
    """The user agent a regular window of this Chrome sends, in Chrome's reduced format."""
    version = subprocess.run([chrome, "--version"], capture_output=True, text=True, check=True).stdout
    major = re.search(r"(\d+)\.\d+\.\d+\.\d+", version)[1]
    platform = PLATFORMS["linux" if sys.platform.startswith("linux") else sys.platform]
    return f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"


async def launch(profile: str, width: int = 1280, height: int = 720) -> CDP:
    """Start Chrome on `profile`, confined so it can only write there and to the temp dirs."""
    chrome = find_chrome()
    # Headless Chrome reports an 800x600 screen, smaller than its own window, which no real desktop has.
    screen = f"{{{max(1920, width)}x{max(1080, height + 120)}}}"
    argv = [chrome, f"--user-data-dir={profile}", f"--user-agent={user_agent(chrome)}",
            f"--window-size={width},{height}", f"--screen-info={screen}", *CHROME_FLAGS]
    cdp = CDP()
    await cdp.start(confine(argv, profile))
    return cdp
