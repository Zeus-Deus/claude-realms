// Refuses Bash commands that re-point GUI tooling at the person's own desktop
// while this session works in a realm (a port of realms_core/host_guard.py).
//
// A realm's tools never see the host display, bus or input handles, but nothing
// stops a command from putting them back: `env WAYLAND_DISPLAY=wayland-1 app`
// is enough for a GUI tool to act on the real desktop while the agent believes
// it is working privately. This is an agent-judgment guardrail, not a
// containment boundary: it reports one recognised pattern out loud.

export const GUARDED_KEYS = [
  'WAYLAND_DISPLAY',
  'HYPRLAND_INSTANCE_SIGNATURE',
  'HYPRLAND_CMD',
  'DISPLAY',
  'XDG_RUNTIME_DIR',
  'DBUS_SESSION_BUS_ADDRESS',
  'AT_SPI_BUS_ADDRESS',
  'SWAYSOCK',
  'I3SOCK',
  'YDOTOOL_SOCKET',
  'CUA_INJECT_SOCKET',
  'CUA_DRIVER_SOCKET',
  'XAUTHORITY',
] as const

const PREFIXES = new Set(['env', 'export', 'declare', 'typeset', 'setenv', ';', '&&', '||', '|', '&'])
const ASSIGNMENT = /^([A-Za-z_][A-Za-z0-9_]*)=([\s\S]*)$/
const OPERATORS = /[;&|]+$/

// Shell words as POSIX sh splits them (quotes, escapes, comments); null when
// the text does not parse, which the guard treats as ordinary.
export function shellWords(text: string): string[] | null {
  const words: string[] = []
  let word = ''
  let hasWord = false
  let i = 0
  while (i < text.length) {
    const c = text[i]!
    if (c === "'") {
      const end = text.indexOf("'", i + 1)
      if (end < 0) return null
      word += text.slice(i + 1, end)
      hasWord = true
      i = end + 1
    } else if (c === '"') {
      i += 1
      let closed = false
      while (i < text.length) {
        const d = text[i]!
        if (d === '\\' && i + 1 < text.length && '"\\$`\n'.includes(text[i + 1]!)) {
          word += text[i + 1]
          i += 2
        } else if (d === '"') {
          closed = true
          i += 1
          break
        } else {
          word += d
          i += 1
        }
      }
      if (!closed) return null
      hasWord = true
    } else if (c === '\\') {
      if (i + 1 < text.length) word += text[i + 1]
      hasWord = true
      i += 2
    } else if (c === '#' && !hasWord) {
      const end = text.indexOf('\n', i)
      i = end < 0 ? text.length : end
    } else if (/\s/.test(c)) {
      if (hasWord) words.push(word)
      word = ''
      hasWord = false
      i += 1
    } else {
      word += c
      hasWord = true
      i += 1
    }
  }
  if (hasWord) words.push(word)
  return words
}

// A guarded key is only an escape when it is pointed somewhere real.
export function isHostValue(key: string, value: string): boolean {
  if (!value) return false
  if (key === 'XDG_RUNTIME_DIR') return /^\/run\/user\/[0-9]+\/?$/.test(value)
  if (key === 'DISPLAY') return /^:[0-9]+(\.[0-9]+)?$/.test(value)
  if (key === 'DBUS_SESSION_BUS_ADDRESS' || key === 'AT_SPI_BUS_ADDRESS')
    return /\/run\/user\/[0-9]+\/(bus|at-spi)/.test(value)
  return true
}

function splitOperator(token: string): [string, boolean] {
  return [token.replace(OPERATORS, ''), OPERATORS.test(token)]
}

// The refusal for a command that re-points a guarded key, else null.
export function hostEscape(command: unknown): string | null {
  if (typeof command !== 'string' || !command.trim()) return null
  const tokens = shellWords(command)
  if (tokens === null) return null
  const found: string[] = []
  tokens.forEach((token, index) => {
    const [bare] = splitOperator(token)
    const match = ASSIGNMENT.exec(bare)
    if (!match) return
    const key = match[1]!
    const value = match[2]!
    if (!(GUARDED_KEYS as readonly string[]).includes(key) || !isHostValue(key, value)) return
    const [previous, ended] = index ? splitOperator(tokens[index - 1]!) : ['', true]
    if (index === 0 || ended || PREFIXES.has(previous) || ASSIGNMENT.test(previous)) found.push(key)
  })
  if (found.length === 0) return null
  return (
    'Blocked: this command re-points ' +
    [...new Set(found)].join(', ') +
    " at the person's own desktop while this session works in a realm. That is how an agent " +
    'ends up driving the real screen while reporting it is working privately. Use realm_exec or ' +
    'realm_launch for the realm, or ask the person (they can run /realm off for host access).'
  )
}
