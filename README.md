# browse-anything

Visit any web page from DSH (or any MCP client). Requests go through [wreq](https://github.com/0x676e67/wreq-python) with a Chrome TLS/HTTP2 fingerprint. Bot walls are handled on the way:

- **Anubis**: the proof of work (`fast`/`slow`) is solved in Python, and `metarefresh` challenges are followed.
- **Cloudflare**: a real Chrome passes the challenge, clicking the Turnstile checkbox as soon as it is shown, and returns the rendered page. Before reading it, Chrome scrolls to the bottom so lazy content loads and waits for the network to go quiet, and it includes shadow DOM, so content added by JavaScript and web components comes through. The `cf_clearance` cookie and Chrome's user agent then go to wreq, so later requests to that site skip the browser.

Chrome always runs in the same sandbox as [DSH's local sandbox](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/sandbox/sandbox-local/src/index.ts) in `workspace-write` mode: Seatbelt (`sandbox-exec`) on macOS, `bwrap` on Linux. It can only write to its throwaway profile and the temp dirs. If no sandbox is available, it refuses to start. It runs in Chrome's new headless mode, the full browser without a window, so nothing ever appears on screen. Its user agent is set to the one a regular Chrome window of the installed version sends, because Cloudflare blocks the default `HeadlessChrome` one.

## Use

```sh
uv run browse-anything https://nitter.kareem.one/aleabitoreddit         # markdown to stdout
uv run browse-anything --html https://nitter.kareem.one/aleabitoreddit  # raw HTML
uv run browse-anything                                                  # MCP server on stdio
```

The MCP server exposes one tool, `browse(url, format="markdown"|"html")`. Chrome or Chromium must be installed. Set `BROWSE_ANYTHING_CHROME` to use a specific binary.

## DeepSeek Harness plugin

This repository is also a DSH plugin bundle. Once installed, the model gets `mcp__browse-anything__browse`. You need [uv](https://docs.astral.sh/uv/), but no manual MCP configuration: the plugin finds uv, runs `uv sync` against the bundled `uv.lock`, and starts the server through DSH's own MCP client.

Install it from the **Plugins** page in the sidebar by entering the absolute path of this checkout (or `github:casper-hansen/browse-anything`). You can also use the CLI:

```sh
dsh plugin --profile <profile> add /path/to/browse-anything
```

A local path is linked, not copied, so edits to the checkout take effect when the server restarts. Changes to `index.js` need a DSH restart.

The plugin's config can be overridden in `~/.dsh/profiles/<profile>/cordis.patch.yml`. The override replaces the whole `config`:

```yaml
- id: browse-anything
  config:
    uv: /opt/homebrew/bin/uv     # default: searched on PATH, ~/.local/bin, ~/.cargo/bin, /opt/homebrew/bin, /usr/local/bin
    chrome: ""                   # Chrome/Chromium binary; default: first one installed
    serverName: browse-anything  # tool name prefix: mcp__<serverName>__browse
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
uv run pytest
```
