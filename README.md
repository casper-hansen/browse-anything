# browse-anything

Visit any web page from DSH (or any MCP client). Requests go through [wreq](https://github.com/0x676e67/wreq-python) with a Chrome TLS/HTTP2 fingerprint. Bot walls are handled on the way:

- **Anubis**: the proof of work (`fast`/`slow`) is solved in Python, and `metarefresh` challenges are followed.
- **Cloudflare**: a real Chrome passes the challenge and clicks Turnstile if one appears. Its `cf_clearance` cookie and user agent then go to wreq, so later requests to that site skip the browser.

Chrome always runs in the same sandbox as [DSH's local sandbox](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/sandbox/sandbox-local/src/index.ts) in `workspace-write` mode: Seatbelt (`sandbox-exec`) on macOS, `bwrap` on Linux. It can only write to its throwaway profile and the temp dirs. If no sandbox is available, it refuses to start. It runs in Chrome's new headless mode, the full browser without a window, so nothing ever appears on screen. Its user agent is set to the one a regular Chrome window of the installed version sends, because Cloudflare blocks the default `HeadlessChrome` one.

## Use

```sh
uv run browse-anything https://nitter.kareem.one/aleabitoreddit         # markdown to stdout
uv run browse-anything --html https://nitter.kareem.one/aleabitoreddit  # raw HTML
uv run browse-anything                                                  # MCP server on stdio
```

The MCP server exposes one tool, `browse(url, format="markdown"|"html")`. To add it to DSH, append to `~/.dsh/profiles/<profile>/cordis.patch.yml`:

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

Chrome or Chromium must be installed. Set `BROWSE_ANYTHING_CHROME` to use a specific binary.

## Test

```sh
uv run pytest
```
