// browse-anything as a DeepSeek Harness plugin.
//
// The Python MCP server in this package does the work. This Host plugin finds the package's own directory and uv,
// installs the server's locked dependencies, and mounts DSH's MCP client on it. A bundle patch alone cannot do this,
// because its `!!js` expressions do not know where the bundle is installed.

import { execFile } from 'node:child_process'
import { accessSync, constants } from 'node:fs'
import { homedir } from 'node:os'
import { delimiter, dirname, isAbsolute, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import z from '@deepseek-ai/schemastery'
import * as mcpClient from '@deepseek-ai/dsh-mcp-client'

export const name = 'browse-anything'

/** The Python project: this package's directory, which holds pyproject.toml and uv.lock. */
const PROJECT = dirname(fileURLToPath(import.meta.url))

export const Config = z.object({
  serverName: z.string().pattern(/^[A-Za-z0-9_-]{1,32}$/).default('browse-anything')
    .description('MCP server name; the tool appears as mcp__<serverName>__browse.'),
  uv: z.string().default('')
    .description('uv executable. Empty searches PATH, then ~/.local/bin, ~/.cargo/bin, /opt/homebrew/bin and /usr/local/bin.'),
  chrome: z.string().default('')
    .description('Chrome or Chromium binary (BROWSE_ANYTHING_CHROME). Empty uses the first one installed.'),
  toolCallTimeoutMs: z.number().min(1).default(120_000)
    .description('Timeout per browse call. Passing Cloudflare and rendering the page can take most of a minute.'),
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
    env: config.chrome ? { BROWSE_ANYTHING_CHROME: config.chrome } : {},
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
