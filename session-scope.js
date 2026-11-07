// Tell the browse-anything server which DSH session each tool call comes from.
//
// One server, and one Chrome, serves every session. MCP does not say which session a call is for, so without this
// two agents browsing at once share one "current tab" and steer each other's page. With it, the server gives each
// session its own tabs. Kept free of imports so it can be tested on its own.

/** The argument the server reads, and removes, to know the caller. It must match CALLER_ARGUMENT in tools.py. */
export const SESSION_ARGUMENT = '_dsh_session'

/** A tool definition whose calls also carry the calling session's id, taken from the DSH execution. */
export function withSession(definition) {
  const execute = definition.execute
  return {
    ...definition,
    execute: (args, exec) => {
      const id = exec?.agent?.id
      const object = typeof args === 'object' && args !== null && !Array.isArray(args) ? args : {}
      return execute(id == null ? object : { ...object, [SESSION_ARGUMENT]: String(id) }, exec)
    },
  }
}

/** A view of DSH's tool registry whose `register` adds the session to every tool it registers. */
export function sessionScopedTools(tools) {
  return new Proxy(tools, {
    get(target, property) {
      if (property === 'register') return definition => target.register(withSession(definition))
      const value = Reflect.get(target, property)
      return typeof value === 'function' ? value.bind(target) : value
    },
  })
}
