# browse-anything

A browser for agents that gets through bot walls. It speaks [Playwright MCP](https://github.com/microsoft/playwright-mcp)'s tool names and arguments, so it can replace Playwright MCP without the model noticing, apart from pages that Playwright gets blocked on now loading.

- **`browser_*` tools**: a long-lived, sandboxed Chrome whose tabs stay open between calls. Navigate, read accessibility snapshots with refs, click, type, fill forms, drag, upload, download, handle dialogs, take screenshots (returned as images), run JavaScript, and inspect console and network.
- **`browse`**: a fast read of a page as markdown or HTML. It goes through [wreq](https://github.com/0x676e67/wreq-python) with a Chrome TLS/HTTP2 fingerprint and uses Chrome only when the page needs it: for a Cloudflare challenge, or a JavaScript app that is empty without it.

## Bot walls

- **Cloudflare**: challenge pages ("Just a moment...") are waited out, and the Turnstile checkbox is clicked with human-like mouse movement when it is shown. This also happens when an action, not just `browser_navigate`, lands on a challenge. A Turnstile widget embedded in a page, such as on a login form, is clicked if it asks for it.
- **Anubis**: Chrome runs the proof of work; `browse` solves it in Python (`fast`/`slow`) and follows `metarefresh` challenges.
- **Other interstitials**: Vercel's checkpoint, DDoS-Guard and similar are waited out.

The browser looks like a regular Chrome: [BrowserScan's bot detection](https://www.browserscan.net/bot-detection) reports *Normal* for every check, including WebDriver, Headless Chrome and CDP. That is because:

- Chrome runs in its new headless mode, the full browser without a window, with the user agent a regular window of the installed version sends. Cloudflare blocks the default `HeadlessChrome` one.
- It is driven over the DevTools pipe without `Runtime.enable`, which [pages can detect](https://github.com/rebrowser/rebrowser-patches#fix-runtimeenable-leak). Helper scripts run in an isolated world the page cannot see.
- Clicks and keys are trusted input events, and the mouse travels to its target.

Some walls are about your IP address rather than your browser. DataDome, for example, may refuse a whole network. Image challenges, such as reCAPTCHA's grid, are left to the model: it can see them with `browser_take_screenshot` and answer with `browser_mouse_click_xy`.

Chrome always runs in the same sandbox as [DSH's local sandbox](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/sandbox/sandbox-local/src/index.ts) in `workspace-write` mode: Seatbelt (`sandbox-exec`) on macOS, `bwrap` on Linux. It can only write to its profile and the temp dirs. If no sandbox is available, it refuses to start.

## Tools

| Tool | |
| --- | --- |
| `browser_navigate`, `browser_navigate_back` | Load a page, past any challenge, and return its snapshot |
| `browser_snapshot`, `browser_find` | The page's accessibility tree with refs (`e12`, `f1e3` inside iframe `f1`), or a search of it |
| `browser_click`, `browser_hover`, `browser_drag`, `browser_type`, `browser_press_key`, `browser_select_option`, `browser_fill_form` | Act on elements by ref or selector |
| `browser_drop`, `browser_file_upload` | Drop files or data on an element; answer a file chooser |
| `browser_mouse_click_xy`, `browser_mouse_move_xy`, `browser_mouse_drag_xy`, `browser_mouse_wheel` | Act at viewport coordinates, for canvases and image grids |
| `browser_take_screenshot` | Viewport, full page, or one element, returned as an image and saved to a file |
| `browser_evaluate` | Run JavaScript on the page or an element |
| `browser_wait_for` | Wait for text to appear or go, or for some time |
| `browser_tabs`, `browser_close` | List, open, select, and close tabs; isolated contexts have their own cookies |
| `browser_handle_dialog` | Accept or dismiss `alert`, `confirm`, and `prompt` |
| `browser_console_messages`, `browser_network_requests`, `browser_network_request` | Console messages; requests, with headers and bodies |
| `browser_resize`, `browser_emulate_media` | Viewport size; color scheme, reduced motion, print media |
| `browse` | Read a page as markdown or HTML without keeping a tab |

Responses use Playwright MCP's sections (`### Result`, `### Page`, `### Snapshot`, `### Modal state`, `### Events`). How they differ from Playwright MCP:

- **Snapshots are smaller.** Unnamed wrapper `div`s that are neither clickable nor focusable are left out, and their children move up. Inline formatting is merged into the text around it, and label text that repeats its field's name is dropped. Refs stay the same across snapshots of a page. They reach into shadow DOM and iframes, including cross-origin ones.
- **Large snapshots go to a file.** After an action, a snapshot over 9,000 characters is saved to a file. The response shows the start of the page's main content, after any open dialogs; a long dialog, such as a cookie banner, is cut down to the start of its text and its buttons. The model can then search the file with `browser_find`, which shows its matches as one tree, or read parts with `browser_snapshot`'s `target` and `depth`. Other large results are saved the same way.
- **Output is kept short.** Same-site links show as paths. Search engines' redirect links (Bing, Google, DuckDuckGo) show where they lead, without ad tracking. Long native dropdowns list their first options with refs and the rest by name. Chrome's own console warnings are counted rather than listed, and the tab list is repeated only when it changes.
- **Covered elements are reported.** A click on an element that something covers, such as a cookie banner, fails and names the cover, with the controls that may dismiss it. An element with no size, such as a custom dropdown's hidden input, is clicked through its enclosing control.
- **New tabs are followed.** When an action opens a tab, such as a link with `target=_blank`, the response switches to it.
- **Turnstile widgets are seen through.** A Turnstile widget in a page, on a login form or a gate like Bing's "One last step", is clicked if it asks and given time to pass.
- **Actions wait for what they start.** After an action, the response waits for the navigation it caused (passing any challenge), for the network to go quiet, and for downloads to finish. Downloads are saved to the output directory.
- **More selector forms are accepted.** `target` takes a ref or a selector: CSS (which also searches open shadow roots), `xpath=` or `//…`, `text=…`, or `role=button[name="Submit"]`.
- **Console capture avoids detection.** Logging `console.log` calls needs `Runtime.enable`, which bot walls can detect. So by default (`console: auto`) the full console is captured only on local and private hosts, such as your dev server. Elsewhere, page errors, unhandled rejections, and Chrome's own messages (failed loads, CSP, deprecations) are captured.
- **`browser_run_code_unsafe`** is not provided: there is no Playwright to run the code in.

## Use

```sh
uv run browse-anything https://nitter.kareem.one/aleabitoreddit         # markdown to stdout
uv run browse-anything --html https://nitter.kareem.one/aleabitoreddit  # raw HTML
uv run browse-anything                                                  # MCP server on stdio
```

Chrome or Chromium must be installed. Playwright's Chromium (`npx playwright install chromium`) is found too. The server reads these environment variables:

| Variable | Default | |
| --- | --- | --- |
| `BROWSE_ANYTHING_CHROME` | first installed | Chrome or Chromium binary |
| `BROWSE_ANYTHING_PROFILE` | throwaway | Persistent profile directory, so logins and clearances survive restarts; one server per profile |
| `BROWSE_ANYTHING_OUTPUT_DIR` | `<tmp>/browse-anything` | Screenshots, downloads, and large output |
| `BROWSE_ANYTHING_CONSOLE` | `auto` | `auto`, `stealth` or `full` (see above) |
| `BROWSE_ANYTHING_VIEWPORT` | `1280x720` | Viewport of new tabs |
| `BROWSE_ANYTHING_TAB_IDLE_MINUTES` | `60` | Close tabs no tool has used for this long; `0` keeps them. One server serves every DSH session, and MCP does not say which session a call is from, so this keeps a new task from starting among an old one's tabs |

Files for `browser_file_upload` and `browser_drop` are copied into Chrome's profile first. Chrome cannot read them where they are when they are in `/tmp`, because the Linux sandbox gives Chrome its own empty `/tmp`.

## DeepSeek Harness plugin

This repository is also a DSH plugin bundle. You need [uv](https://docs.astral.sh/uv/), but no manual MCP configuration: the plugin finds uv, runs `uv sync` against the bundled `uv.lock`, and starts the server through DSH's own MCP client.

Install it from the **Plugins** page in the sidebar by entering the absolute path of this checkout (or `github:casper-hansen/browse-anything`). You can also use the CLI:

```sh
dsh plugin --profile <profile> add /path/to/browse-anything
```

A local path is linked, not copied, so edits to the checkout take effect when the server restarts. Changes to `index.js` need a DSH restart.

### Replacing Playwright MCP

To have browse-anything answer the `mcp__browser__*` tools a Playwright MCP entry provided, give it that entry's server name and disable the entry. In `~/.dsh/profiles/<profile>/cordis.patch.yml`:

```yaml
- id: mcp-browser        # the id of your Playwright MCP entry
  disabled: true
- id: browse-anything
  config:
    serverName: browser
```

### Configuration

The plugin's config can be overridden in `~/.dsh/profiles/<profile>/cordis.patch.yml`. The override replaces the whole `config`:

```yaml
- id: browse-anything
  config:
    serverName: browse-anything  # tool name prefix: mcp__<serverName>__browser_navigate, ...
    uv: /opt/homebrew/bin/uv     # default: searched on PATH, ~/.local/bin, ~/.cargo/bin, /opt/homebrew/bin, /usr/local/bin
    chrome: ""                   # Chrome/Chromium binary; default: first one installed
    profile: ""                  # persistent Chrome profile directory; default: a throwaway one
    outputDir: ""                # screenshots, downloads, large output; default: <tmp>/browse-anything
    console: auto                # auto | stealth | full
    viewport: 1280x720
    tabIdleMinutes: 60           # close tabs unused this long; 0 keeps them
    toolCallTimeoutMs: 120000
    syncTimeoutMs: 600000        # first `uv sync` downloads the Python dependencies
    failOnStartupError: true
```

If uv or the server can't start, DSH still boots. The plugin shows as not activated, with the error in the startup warnings.

### Without the plugin

Any MCP client can run the server directly. In DSH, append this to `cordis.patch.yml`:

```yaml
- insert:
    - id: mcp-browse-anything
      name: "@deepseek-ai/dsh-mcp-client"
      config:
        serverName: browse-anything
        transport: stdio
        command: uv
        args: [run, --directory, /path/to/browse-anything, browse-anything]
        toolCallTimeoutMs: 120000
```

## Test

```sh
uv run pytest                                          # offline
BROWSE_ANYTHING_LIVE=1 uv run pytest tests/test_live.py  # MCP over stdio against a real Chrome, and Cloudflare
```
