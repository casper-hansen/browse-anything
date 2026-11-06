// browse-anything as a DeepSeek Harness plugin.
//
// The Python MCP server in this package does the work: Playwright MCP's browser_* tools plus `browse`, on a sandboxed
// Chrome that gets past Cloudflare, Turnstile and Anubis. This Host plugin finds the package's own directory and uv,
// installs the server's locked dependencies, and mounts DSH's MCP client on it. A bundle patch alone cannot do this,
// because its `!!js` expressions do not know where the bundle is installed.

import { execFile } from 'node:child_process'
import { accessSync, constants, realpathSync } from 'node:fs'
import { createRequire } from 'node:module'
import { homedir } from 'node:os'
import { delimiter, dirname, isAbsolute, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

/**
 * A peer dependency: from this package's own resolution when it is installed into a profile, else (a checkout that
 * is linked or inserted by path has no node_modules of its own) from the running DSH's.
 */
async function peer(specifier) {
  try {
    return await import(specifier)
  } catch (error) {
    if (error?.code !== 'ERR_MODULE_NOT_FOUND' || !process.argv[1]) throw error
    const fromHost = createRequire(realpathSync(process.argv[1]))
    return await import(pathToFileURL(fromHost.resolve(specifier)).href)
  }
}

const { default: z } = await peer('@deepseek-ai/schemastery')
const mcpClient = await peer('@deepseek-ai/dsh-mcp-client')

export const name = 'browse-anything'

/** The Python project: this package's directory, which holds pyproject.toml and uv.lock. */
const PROJECT = dirname(fileURLToPath(import.meta.url))

export const Config = z.object({
  serverName: z.string().pattern(/^[A-Za-z0-9_-]{1,32}$/).default('browse-anything')
    .description('MCP server name; tools appear as mcp__<serverName>__browser_navigate and so on. Set it to `browser` (and disable the Playwright MCP entry) to stand in for Playwright MCP under the same tool names.'),
  uv: z.string().default('')
    .description('uv executable. Empty searches PATH, then ~/.local/bin, ~/.cargo/bin, /opt/homebrew/bin and /usr/local/bin.'),
  chrome: z.string().default('')
    .description('Chrome or Chromium binary (BROWSE_ANYTHING_CHROME). Empty uses the first one installed, including Playwright\'s Chromium.'),
  profile: z.string().default('')
    .description('Persistent Chrome profile directory, so logins and site clearances survive restarts. Empty uses a throwaway profile. One server per profile.'),
  outputDir: z.string().default('')
    .description('Where screenshots, downloads, and snapshots too large to return are saved. Empty uses <tmp>/browse-anything.'),
  console: z.union([z.const('auto'), z.const('stealth'), z.const('full')]).default('auto')
    .description('Console capture. `full` records console.log calls but uses Runtime.enable, which bot walls can detect; `stealth` records only page errors and browser messages; `auto` is full on local and private hosts, stealth elsewhere.'),
  viewport: z.string().pattern(/^\d+x\d+$/).default('1280x720')
    .description('Viewport size of new tabs, WIDTHxHEIGHT.'),
  toolCallTimeoutMs: z.number().min(1).default(120_000)
    .description('Timeout per tool call. Passing Cloudflare and rendering a page can take most of a minute.'),
  syncTimeoutMs: z.number().min(1).default(600_000)
    .description('Timeout for installing the Python dependencies with uv sync; the first install downloads them.'),
  failOnStartupError: z.boolean().default(true)
    .description('Fail plugin activation when the server cannot start, instead of only logging it.'),
})

export async function apply(ctx, config) {
  const uv = findUv(config.uv)
  const abort = new AbortController()
  ctx.effect(() => () => abort.abort())

  // Installed up front, because `uv run` would otherwise download the dependencies inside the MCP client's
  // 60-second connection timeout. `--inexact` keeps dev packages a checkout already has.
  await run(uv, ['sync', '--frozen', '--no-dev', '--inexact', '--project', PROJECT], config.syncTimeoutMs, abort.signal)

  await ctx.plugin(mcpClient, {
    transport: 'stdio',
    serverName: config.serverName,
    command: uv,
    args: ['run', '--no-sync', '--project', PROJECT, 'browse-anything'],
    cwd: PROJECT,
    env: {
      ...(config.chrome ? { BROWSE_ANYTHING_CHROME: config.chrome } : {}),
      ...(config.profile ? { BROWSE_ANYTHING_PROFILE: config.profile } : {}),
      ...(config.outputDir ? { BROWSE_ANYTHING_OUTPUT_DIR: config.outputDir } : {}),
      BROWSE_ANYTHING_CONSOLE: config.console,
      BROWSE_ANYTHING_VIEWPORT: config.viewport,
    },
    toolCallTimeoutMs: config.toolCallTimeoutMs,
    failOnStartupError: config.failOnStartupError,
  })
}

/** The uv executable: the configured one, else the first found on PATH or where uv's installers put it. */
function findUv(configured) {
  if (configured) return configured
  const home = homedir()
  const dirs = [
    ...(process.env.PATH ?? '').split(delimiter),
    join(home, '.local', 'bin'),
    join(home, '.cargo', 'bin'),
    '/opt/homebrew/bin',
    '/usr/local/bin',
  ]
  const names = process.platform === 'win32' ? ['uv.exe'] : ['uv']
  for (const dir of dirs) {
    if (!dir || !isAbsolute(dir)) continue
    for (const file of names.map(name => join(dir, name))) {
      try {
        accessSync(file, constants.X_OK)
        return file
      } catch {}
    }
  }
  throw new Error('browse-anything: uv not found. Install it (https://docs.astral.sh/uv/) or set `uv` in this plugin\'s config.')
}

function run(command, args, timeout, signal) {
  return new Promise((resolve, reject) => {
    execFile(command, args, { cwd: PROJECT, timeout, signal, maxBuffer: 16 << 20 }, (error, stdout, stderr) => {
      if (!error) return resolve()
      const output = `${stderr}${stdout}`.trim().split('\n').slice(-20).join('\n')
      reject(new Error(`browse-anything: \`${command} ${args.join(' ')}\` failed: ${error.message}${output ? `\n${output}` : ''}`))
    })
  })
}
