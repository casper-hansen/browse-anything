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


def brands(major: int, version: str, product: str = "Google Chrome") -> list[dict]:
    """Chrome's user-agent brand list, as components/embedder_support/user_agent_utils.cc builds it: a GREASE
    brand, Chromium, and the product, shuffled by a permutation seeded with the major version."""
    chars = [" ", "(", ":", "-", ".", "/", ")", ";", "=", "?", "_"]
    grease_version = ["8", "99", "24"][major % 3]
    full = version.count(".") == 3
    entries = [(f"Not{chars[major % 11]}A{chars[(major + 1) % 11]}Brand", grease_version + (".0.0.0" if full else "")),
               ("Chromium", version), (product, version)]
    order = [(0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0)][major % 6]
    shuffled = [None] * 3
    for i, entry in enumerate(entries):
        shuffled[order[i]] = {"brand": entry[0], "version": entry[1]}
    return shuffled


def client_hints(chrome: str) -> dict | None:
    """User-agent client hints of a regular Google Chrome of this version, when `chrome` is another build.

    Chromium and Chrome for Testing (Playwright's) report only "Chromium" in navigator.userAgentData and the
    Sec-CH-UA headers, while the user agent says Chrome: a known sign of automation. Real Google Chrome needs none.
    """
    version_text = subprocess.run([chrome, "--version"], capture_output=True, text=True, check=True).stdout.strip()
    if re.match(r"Google Chrome \d", version_text):
        return None
    full = re.search(r"(\d+)\.\d+\.\d+\.\d+", version_text)
    major = int(full[1])
    linux = sys.platform.startswith("linux")
    release = re.match(r"\d+(\.\d+){0,2}", os.uname().release)
    return {
        "brands": brands(major, str(major)),
        "fullVersionList": brands(major, full[0]),
        "fullVersion": full[0],
        "platform": "Linux" if linux else "macOS",
        "platformVersion": (release[0] if linux and release else "") if linux else "15.0.0",
        "architecture": "x86" if os.uname().machine in ("x86_64", "AMD64") else "arm",
        "bitness": "64",
        "model": "",
        "mobile": False,
        "wow64": False,
        "formFactors": ["Desktop"],
    }


async def launch(profile: str, width: int = 1280, height: int = 720) -> CDP:
    """Start Chrome on `profile`, confined so it can only write there and to the temp dirs."""
    chrome = find_chrome()
    # Headless Chrome reports an 800x600 screen, smaller than its own window, which no real desktop has.
    screen = f"{{{max(1920, width)}x{max(1080, height + 120)}}}"
    argv = [chrome, f"--user-data-dir={profile}", f"--user-agent={user_agent(chrome)}",
            f"--window-size={width},{height}", f"--screen-info={screen}", *CHROME_FLAGS]
    cdp = CDP()
    await cdp.start(confine(argv, profile))
    cdp.user_agent, cdp.client_hints = user_agent(chrome), client_hints(chrome)
    return cdp
