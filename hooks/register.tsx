// claude-realms: this session's private Linux desktop, live in a pane.
//
// The realm itself (labwc desktop or Omarchy VM) and the agent's desktop
// tools live in the plugin's MCP server. This module is the person's side of
// it: the /realm command, a pane that streams the realm's screen (pixels in
// kitty/Ghostty, half-block cells in any other terminal), taking control of
// the desktop, a status line, and a guard that keeps Bash commands from
// re-pointing GUI tools at the person's own screen while a realm is in use.

import type { EngineInterface, PluginOptions, Register } from 'claude-code'
import { hostEscape } from './guard'

type $ = EngineInterface

const PANE = 'realm'
const SERVER = 'realms'
const STATUS_POLL_MS = 3000
const INPUT_HISTORY = 128

type Live = { id: string; kind: string; size?: string | null; memory_mb?: number | null; vnc_socket?: string | null }
type Job = { state: string; log: string[]; error?: string }
type Status = {
  enabled: boolean
  kind: string
  live: Live | null
  setup: { ready: boolean; message: string; install_command?: string | null; steps: string[] }
  frames_argv: string[]
  input_path?: string
  controlled?: boolean
  jobs?: Record<string, Job>
  events?: { at: number; text: string }[]
  driver_tools?: number
}
type Picture =
  | { kind: 'image'; source: { file: string; format: 'rgb'; width: number; height: number; generation: number } }
  | { kind: 'raster'; columns: number; rows: number; cells: string }
type Mode = 'image' | 'raster'
type Control = { pid: number; socket: string; python: string; client: string; ancestors: { pid: number }[] }

// The plugin's server: its tools' name prefix as Claude Code lists them, and
// how the mod reaches it for the person's own actions
const toolPrefix = 'mcp__plugin_realms_realms__'
let control: Control | null = null
let connectError: string | null = null
let status: Status | null = null
let poller: { cancel: () => void } | null = null

// The pane and its stream
let isPaneOpen = false
let isDismissed = false
let stream: AsyncGenerator<unknown, unknown, unknown> | null = null
let streamGeneration = 0
let streamSocket: string | null = null
let streamBox = { columns: 0, rows: 0 }
let mode: Mode = 'image'
let modeLocked = false
let picture: Picture | null = null
let framebuffer = { width: 1920, height: 1080 }
let viewError: string | null = null
let box = { columns: 80, rows: 22 }
let restartTimer: { cancel: () => void } | null = null

// Taking control
let isControlled = false
let inputEvents: { [key: string]: unknown }[] = []
let inputId = 0
let inputEpoch = ''


// Setup jobs the server is running, to report how they ended
let fastPoller: { cancel: () => void } | null = null
let callsInFlight = 0
let runningJobs = new Set<string>()

// The terminal: Codemux (its browser pane hosts the full view) and the shape
// of a text cell (height / width), which keeps the realm's 16:9 picture 16:9.
let inCodemux = false
// Inside herdr, which can carry pictures to a kitty/Ghostty terminal but is
// not recognised by Claude Code's image check, and whether pictures were refused
let inHerdr = false
let imagesRefused = false
let cellAspect = 2.1
// The realm a Codemux browser pane already shows, so it opens once per realm
let browserShownFor: string | null = null

// Where the realm is watched: the terminal pane, or (in Codemux, whose
// terminal draws no pictures) a Codemux browser pane at full resolution.
function viewsInBrowser(): boolean {
  const where = option<string>('view_in', 'auto')
  return where === 'browser' || (where === 'auto' && inCodemux)
}

let options: PluginOptions = {}

function option<T extends string | number | boolean>(name: string, fallback: T): T {
  const value = options[name]
  return (typeof value === typeof fallback ? value : fallback) as T
}

// ------------------------------------------------------------------ server
//
// The person's actions reach the plugin's server over its private control
// socket, not through $.mcp: Claude Code routes a plugin's own MCP calls
// through the tool permission flow, which would ask the person to approve
// their own clicks. The server publishes, under XDG_RUNTIME_DIR, which
// processes it descends from; the one below this Claude Code process is ours.

async function locate($: $): Promise<Control | null> {
  if (control && (await $.fs.exists(control.socket))) return control
  control = null
  const probe = await $.process.run(['/bin/sh', '-c', 'echo "$PPID $(id -u)"'])
  const [pid, uid] = probe.stdout.trim().split(' ').map(Number)
  const runtime = (await $.env.get('XDG_RUNTIME_DIR')) || '/tmp'
  for (const base of [runtime, '/tmp']) {
    const directory = base + '/claude-realms-' + uid
    if (!(await $.fs.exists(directory))) continue
    const entries = await $.fs.list(directory)
    const candidates: Control[] = []
    for (const entry of entries) {
      if (!entry.name.endsWith('.json')) continue
      try {
        const data = JSON.parse(await $.fs.read(directory + '/' + entry.name)) as Control
        if (data.ancestors.some((process) => process.pid === pid) && (await $.fs.exists(data.socket))) candidates.push(data)
      } catch {}
    }
    candidates.sort((a, b) => b.pid - a.pid)
    if (candidates[0]) {
      control = candidates[0]
      connectError = null
      return control
    }
  }
  connectError = 'the realms server has not started yet (it starts with the session; /mcp shows its state)'
  return null
}

const SLOW = new Set(['on', 'setup', 'doctor', 'shot', 'driver', 'push', 'pull', 'stop', 'off'])

async function realm($: $, args: Record<string, unknown>): Promise<{ text: string; data: unknown; isError: boolean }> {
  const found = await locate($)
  if (!found) return { text: 'Realms: ' + connectError, data: null, isError: true }
  const timeoutMs = SLOW.has(String(args.action)) ? 10 * 60 * 1000 : 30 * 1000
  const result = await $.process.run([found.python, '-I', '-S', found.client, found.socket, JSON.stringify(args)], { timeoutMs })
  let reply: { texts?: string[]; isError?: boolean; unreachable?: boolean } = {}
  try {
    reply = JSON.parse(result.stdout)
  } catch {
    return { text: 'Realms: no reply from the server ' + result.stderr.trim(), data: null, isError: true }
  }
  if (reply.unreachable) control = null
  const [first = '', second] = reply.texts ?? []
  let data: unknown = null
  if (second !== undefined) {
    try {
      data = JSON.parse(second)
    } catch {
      data = second
    }
  }
  return { text: first, data, isError: Boolean(reply.isError) }
}

async function refresh($: $): Promise<Status | null> {
  try {
    const { data, isError } = await realm($, { action: 'status' })
    if (!isError && data && typeof data === 'object') status = data as Status
  } catch (error) {
    $.ui.log('claude-realms: status failed: ' + String(error), { to: 'debug' })
  }
  isControlled = status?.controlled ?? isControlled
  showStatusLine($)
  await followJobs($)
  await syncStream($)
  return status
}

function showStatusLine($: $) {
  // The footer toggle (SessionMode) shows the realm's state; it redraws here.
  $.ui.invalidate('ui.render')
}

// The realm at full resolution in the browser (noVNC), with control.
async function openFullView($: $) {
  const { text, data, isError } = await realm($, { action: 'watch', control: true })
  const url = (data as { url?: string } | null)?.url
  if (isError || !url) {
    $.ui.toast(text)
    return
  }
  const where = option<string>('full_view', 'auto')
  if ((where === 'auto' && inCodemux) || where === 'codemux') {
    // Codemux's own browser pane: full resolution, beside the conversation.
    let pane = await $.process.run(['codemux', 'browser', 'open', url], { timeoutMs: 15000 }).catch(() => ({ exitCode: 1 }))
    if (pane.exitCode !== 0) {
      // No browser pane yet in this workspace: make one, then show the realm.
      await $.process.run(['codemux', 'browser', 'create'], { timeoutMs: 15000 }).catch(() => undefined)
      pane = await $.process.run(['codemux', 'browser', 'open', url], { timeoutMs: 15000 }).catch(() => ({ exitCode: 1 }))
    }
    if (pane.exitCode === 0) {
      browserShownFor = status?.live?.id ?? null
      $.ui.toast('The realm is open in a Codemux browser pane')
      return
    }
  }
  const opened = await $.process.run(['xdg-open', url], { timeoutMs: 10000 }).catch(() => ({ exitCode: 1 }))
  if (opened.exitCode === 0) {
    $.ui.toast('Full view opened in your browser')
  } else {
    await $.ui.copy({ text: url }).catch(() => undefined)
    $.ui.toast('Full view link copied to the clipboard')
  }
}

async function togglePane($: $) {
  if (isPaneOpen) await closePane($)
  else if (viewsInBrowser() && status?.live) await openFullView($)
  else await openPane($, { asked: true })
}

function startPolling($: $) {
  if (poller) return
  poller = $.clock.every(STATUS_POLL_MS, () => {
    if (!isPaneOpen && !status?.live) {
      poller?.cancel()
      poller = null
      return
    }
    void refresh($)
  })
}

// ------------------------------------------------------------------ stream

function isOurTool(tool: string): boolean {
  return tool.startsWith(toolPrefix)
}

async function syncStream($: $) {
  const socket = isPaneOpen ? status?.live?.vnc_socket ?? null : null
  if (!socket) {
    if (stream) await stopStream($)
    if (!status?.live) picture = null
    return
  }
  if (stream && streamSocket === socket) return
  await stopStream($)
  void runStream($, socket)
}

async function stopStream($: $) {
  streamGeneration += 1
  const current = stream
  stream = null
  streamSocket = null
  restartTimer?.cancel()
  restartTimer = null
  // Ending the stream's loop ends the viewer process. Not awaited: a stream
  // is only interruptible at its next piece, and the generation check above
  // already drops anything a stopped stream still says.
  if (current) void current.return(undefined).catch(() => undefined)
  void $
}

function restartStream($: $) {
  restartTimer?.cancel()
  restartTimer = $.clock.after(50, async () => {
    restartTimer = null
    picture = null
    await stopStream($)
    await syncStream($)
    $.ui.invalidate('ui.render')
  })
}

function setMode($: $, next: Mode, why?: string) {
  if (mode === next) return
  mode = next
  if (why) $.ui.log('claude-realms: live view switched to ' + next + ': ' + why, { to: 'debug' })
  restartStream($)
}

async function runStream($: $, socket: string) {
  if (!status) return
  const generation = ++streamGeneration
  const requested = { ...box }
  const argv = [
    ...status.frames_argv,
    '--socket', socket,
    '--mode', mode,
    '--columns', String(requested.columns),
    '--rows', String(requested.rows),
    '--fps', String(option('view_fps', 10)),
    ...(status.input_path ? ['--input', status.input_path] : []),
  ]
  const child = $.process.spawn({ argv }) as unknown as AsyncGenerator<{ stream: string; text: string }, unknown, unknown>
  stream = child
  streamSocket = socket
  streamBox = requested
  viewError = null
  let pending = ''
  try {
    for await (const { stream: pipe, text } of child) {
      if (generation !== streamGeneration) break
      if (pipe !== 'stdout') continue
      const lines = (pending + text).split('\n')
      pending = lines.pop() ?? ''
      for (const line of lines) await onLine($, line)
    }
  } catch (error) {
    viewError = 'the live view stopped: ' + String(error)
  } finally {
    if (stream === child) {
      stream = null
      streamSocket = null
    }
  }
  if (generation === streamGeneration && isPaneOpen) $.ui.invalidate('ui.render')
}

async function onLine($: $, line: string) {
  if (line.startsWith('@hello ')) {
    const hello = JSON.parse(line.slice(7)) as { width: number; height: number }
    framebuffer = { width: hello.width, height: hello.height }
    $.ui.invalidate('ui.render')
    return
  }
  if (line.startsWith('@size ')) {
    const [, w, h] = line.split(' ')
    framebuffer = { width: Number(w), height: Number(h) }
    restartStream($)
    return
  }
  if (line.startsWith('@error ')) {
    viewError = line.slice(7)
    $.ui.invalidate('ui.render')
    return
  }
  const frame = /^@file (\S+) (\d+) (\d+) (\d+)$/.exec(line)
  if (frame) {
    const source = { file: frame[1]!, format: 'rgb' as const, width: Number(frame[2]), height: Number(frame[3]), generation: Number(frame[4]) }
    if (picture?.kind !== 'image') {
      picture = { kind: 'image', source }
      $.ui.invalidate('ui.render')
      return
    }
    picture = { kind: 'image', source }
    const result = await $.ui.blit({ requestId: PANE, key: 'view', source })
    // Only refusals that mean "this terminal shows no pictures" switch to
    // coloured cells; others (a redraw in flight, a busy surface) pass.
    if (result.deny && /draws no placeholder images|cannot read files on this machine|8-bit image id/.test(result.deny)) {
      imagesRefused = true
      if (!modeLocked) setMode($, 'raster', result.deny)
    }
    return
  }
  const raster = /^@raster (\d+) (\d+) (\S+)$/.exec(line)
  if (raster) {
    const columns = Number(raster[1])
    const rows = Number(raster[2])
    const cells = raster[3]!
    if (columns !== box.columns || rows !== box.rows) {
      restartStream($)
      return
    }
    if (picture?.kind !== 'raster' || picture.columns !== columns || picture.rows !== rows) {
      picture = { kind: 'raster', columns, rows, cells }
      $.ui.invalidate('ui.render')
      return
    }
    picture = { kind: 'raster', columns, rows, cells }
    const result = await $.ui.blit({ requestId: PANE, key: 'view', cells })
    if (result.deny) restartStream($)
  }
}

// Fit the realm's aspect into the pane, knowing how tall a text cell is
// against its width (about 2 in kitty and Ghostty, 2.4 in Codemux).
function fit(bodyColumns: number, viewportRows: number, maxColumns: number, maxRows: number) {
  const room = Math.max(6, Math.min(maxRows, viewportRows - 8))
  let columns = Math.max(16, Math.min(maxColumns, bodyColumns))
  let rows = Math.max(4, Math.round((columns * framebuffer.height) / framebuffer.width / cellAspect))
  if (rows > room) {
    rows = room
    columns = Math.max(16, Math.min(columns, Math.round((rows * cellAspect * framebuffer.width) / framebuffer.height)))
  }
  return { columns, rows }
}

// ------------------------------------------------------------------ setup

function startFastPolling($: $) {
  if (fastPoller) return
  fastPoller = $.clock.every(500, () => {
    if (callsInFlight === 0 && runningJobs.size === 0) {
      fastPoller?.cancel()
      fastPoller = null
      return
    }
    void refresh($)
  })
}

// Follow the server's setup jobs and say how they ended.
async function followJobs($: $) {
  const jobs = status?.jobs ?? {}
  const running = new Set(Object.entries(jobs).filter(([, job]) => job.state === 'running').map(([name]) => name))
  if (running.size) startFastPolling($)
  for (const name of runningJobs) {
    if (running.has(name)) continue
    const job = jobs[name]
    if (job?.state === 'failed') $.ui.toast('Realm setup failed: ' + (job.error ?? 'unknown error'))
  }
  runningJobs = running
}

// ------------------------------------------------------------------- pane

async function openPane($: $, { asked }: { asked: boolean }) {
  const opened = await $.ui.open({ id: PANE, title: 'Realm' })
  if (!opened.isPlaced && !asked) {
    // Opened unasked in a narrow terminal it would pop up later; offer instead.
    await $.ui.close({ id: PANE })
    $.ui.toast('The realm is live · /realm view to watch it')
    return false
  }
  isPaneOpen = true
  isDismissed = false
  startPolling($)
  await refresh($)
  return true
}

async function closePane($: $) {
  await $.ui.close({ id: PANE })
}

async function takeControl($: $, held: boolean) {
  const { isError, text } = await realm($, { action: 'control', control: held })
  if (isError) {
    $.ui.toast(text)
    return
  }
  isControlled = held
  inputEvents = []
  inputId = 0
  // A fresh epoch tells the viewer that ids start over for this takeover.
  inputEpoch = held ? String(Date.now()) + '-' + Math.random().toString(36).slice(2, 8) : ''
  if (held) {
    await $.ui.focus({ requestId: PANE, key: 'takeover' }).catch(() => undefined)
  }
  showStatusLine($)
  $.ui.invalidate('ui.render')
}

async function writeInput($: $, events: { [key: string]: unknown }[]) {
  if (!status?.input_path || !isControlled) return
  for (const event of events) inputEvents.push({ ...event, id: ++inputId })
  inputEvents = inputEvents.slice(-INPUT_HISTORY)
  await $.fs.write(status.input_path, JSON.stringify({ epoch: inputEpoch, events: inputEvents }))
}

// ---------------------------------------------------------------- command

const USAGE = [
  '/realm              status of this session\'s realm',
  '/realm view [image|raster]   watch it live in a pane',
  '/realm on [omarchy] start it (regular realm, or the Omarchy VM)',
  '/realm off          stop it and turn agent use off for this session',
  '/realm stop         power it down, keep its home',
  '/realm control      take or hand back control of its desktop',
  '/realm full         open it full size in your browser (noVNC)',
  '/realm watch        copy that browser link instead',
  '/realm setup [omarchy]   install what it needs',
  '/realm size WxH     resize the regular realm',
  '/realm launch CMD   start a program on the realm desktop',
  '/realm repair       reconnect the desktop driver, keep the desktop',
  '/realm list         realms and VM disks kept on this machine, with sizes',
  '/realm delete ID    delete one stopped realm or VM disk',
  '/realm clean        delete every stopped realm except this session\'s',
  '/realm driver [check|update|rollback]',
  '/realm doctor',
].join('\n')

function describeSetup(data: unknown, started: boolean): string {
  const setup = (data as { setup?: Status['setup'] } | null)?.setup
  if (!setup) return ''
  // Once the setup job runs, its own progress (in /realm status) says the rest.
  const lines = started ? [] : [setup.message]
  if (setup.install_command) lines.push('Run: ! ' + setup.install_command)
  return lines.join('\n')
}

async function command($: $, raw: string): Promise<string> {
  const [verb = 'status', ...rest] = raw.trim().split(/\s+/).filter(Boolean)
  switch (verb) {
    case 'help':
      return USAGE
    case 'status': {
      const { text, data, isError } = await realm($, { action: 'status' })
      if (!isError && data) status = data as Status
      showStatusLine($)
      const jobs = Object.entries(status?.jobs ?? {})
        .map(([name, job]) => '  ' + name + ': ' + job.state + (job.error ? ' (' + job.error + ')' : job.log.length ? ' · ' + job.log.at(-1) : ''))
        .join('\n')
      const events = (status?.events ?? []).map((event) => '  ' + event.text).join('\n')
      return [text, jobs && 'Setup jobs:\n' + jobs, events && 'Recent:\n' + events, '/realm help lists commands.'].filter(Boolean).join('\n')
    }
    case 'view': {
      if (rest[0] === 'image' || rest[0] === 'raster') {
        modeLocked = true
        setMode($, rest[0])
      } else if (rest[0] === 'auto') {
        modeLocked = false
      }
      if (viewsInBrowser() && !rest[0] && status?.live) {
        await openFullView($)
        return 'Watching ' + status.live.id + ' at full resolution in a Codemux browser pane. (/realm view raster shows the small cell preview here.)'
      }
      await openPane($, { asked: true })
      return status?.live ? 'Watching ' + status.live.id + ' in the Realm pane.' : 'Realm pane open; nothing is running yet.'
    }
    case 'hide':
      await closePane($)
      return 'Realm pane closed.'
    case 'on': {
      const kind = rest[0] === 'omarchy' || rest[0] === 'vm' ? 'omarchy' : 'realm'
      const { text, isError } = await realm($, { action: 'on', kind })
      await refresh($)
      if (!isError) {
        if (viewsInBrowser() && status?.live) await openFullView($)
        else await openPane($, { asked: true })
      }
      return text
    }
    case 'off':
    case 'stop': {
      const { text } = await realm($, { action: verb })
      if (isControlled) isControlled = false
      await refresh($)
      return text
    }
    case 'control':
      await takeControl($, !isControlled)
      return isControlled ? 'You hold the realm desktop; the agent waits. /realm control hands it back.' : 'The agent has the realm desktop again.'
    case 'full':
      await openFullView($)
      return 'Opening the full view…'
    case 'watch': {
      const { text, data, isError } = await realm($, { action: 'watch', control: rest[0] === 'control' })
      const url = (data as { url?: string } | null)?.url
      if (!isError && url) {
        const copied = await $.ui.copy({ text: url }).catch(() => ({ isCopied: false }))
        return text + ((copied as { isCopied?: boolean }).isCopied ? '\n(link copied to the clipboard)' : '')
      }
      return text
    }
    case 'setup': {
      const { text, data } = await realm($, { action: 'setup', ...(rest[0] ? { kind: rest[0] === 'omarchy' ? 'omarchy' : 'realm' } : {}) })
      startPolling($)
      await refresh($)
      const started = ((data as { started?: string[] } | null)?.started ?? []).length > 0
      return [text, describeSetup(data, started)].filter((line, index, all) => line && all.indexOf(line) === index).join('\n')
    }
    case 'size': {
      const { text } = await realm($, { action: 'size', size: rest[0] ?? '' })
      await refresh($)
      return text
    }
    case 'list': {
      const { text } = await realm($, { action: 'list' })
      return text
    }
    case 'delete': {
      if (!rest[0]) return 'Usage: /realm delete ID (see /realm list)'
      const { text } = await realm($, { action: 'delete', id: rest[0] })
      return text
    }
    case 'clean': {
      const { text } = await realm($, { action: 'clean' })
      return text
    }
    case 'repair': {
      const { text } = await realm($, { action: 'repair' })
      return text
    }
    case 'launch': {
      const { text } = await realm($, { action: 'launch', command: rest.join(' ') })
      return text
    }
    case 'driver': {
      const { text } = await realm($, { action: 'driver', operation: rest[0] ?? 'status' })
      return text
    }
    case 'doctor': {
      const { text } = await realm($, { action: 'doctor' })
      return text
    }
    case 'shot': {
      const { text, isError } = await realm($, { action: 'shot' })
      return isError ? text : 'Captured the realm screen (the agent can see it with realm shot).'
    }
    default:
      return 'Unknown: /realm ' + raw + '\n' + USAGE
  }
}

// --------------------------------------------------------------- register

export const register: Register = (on, pluginOptions) => {
  options = pluginOptions
  mode = option<string>('view_mode', 'auto') === 'raster' ? 'raster' : 'image'
  modeLocked = option<string>('view_mode', 'auto') !== 'auto'

  on('session.start', async ($, e, next) => {
    // Codemux marks its terminal panes (CODEMUX=1, TERM_PROGRAM=codemux). Its
    // terminal is xterm.js, which shows no pictures, so the realm's view there
    // is a Codemux browser pane instead.
    inCodemux = (await $.env.get('CODEMUX')) === '1' || (await $.env.get('TERM_PROGRAM')) === 'codemux'
      || Boolean(await $.env.get('CODEMUX_PANE_ID'))
    const aspect = option<number>('view_cell_aspect', 0)
    cellAspect = aspect >= 1 && aspect <= 4 ? aspect : inCodemux ? 2.4 : 2.1
    inHerdr = Boolean(await $.env.get('HERDR_PANE_ID'))
    await $.command.register({
      name: 'realm',
      description: "This session's private Linux desktop: status, view, on/off, control, setup",
      argumentHint: 'view | on [omarchy] | off | stop | control | watch | list | delete ID | clean | setup [omarchy] | launch CMD | driver | doctor',
    })
    void (async () => {
      // The server starts with the session; give it a moment to publish itself.
      for (let attempt = 0; attempt < 20 && !(await locate($)); attempt++) await $.clock.sleep(500)
      await refresh($)
      if (status?.live) startPolling($)
    })()
    return next(e)
  })

  on('command.run', { command: 'realm' }, async ($, e) => {
    try {
      return { text: await command($, e.args) }
    } catch (error) {
      return { text: 'claude-realms: ' + String(error) }
    }
  })

  // The agent used the realm: pick up the new state, and show the desktop the
  // first time it goes live unless the person closed the pane this session.
  on('tool.call', async ($, e, next) => {
    if (!isOurTool(e.tool)) return next(e)
    // Only Claude Code knows which loop made a call: tell the server when a
    // subagent did, so one that asked for its own realm gets it.
    const agentId = (e as { agentId?: string }).agentId
    const wasLive = Boolean(status?.live)
    callsInFlight++
    startFastPolling($)
    let result
    try {
      result = await next(agentId ? ({ ...e, _agent: agentId } as typeof e) : e)
    } finally {
      callsInFlight--
    }
    await refresh($)
    if (!wasLive && status?.live && !isDismissed && option('auto_view', true)) {
      if (viewsInBrowser()) {
        if (browserShownFor !== status.live.id) void openFullView($)
      } else if (!isPaneOpen) {
        void openPane($, { asked: false })
      }
    }
    if (status?.live) startPolling($)
    return result
  })

  on('tool.call', { tool: 'Bash' }, ($, e, next) => {
    if (!status?.live || !option('host_guard', true)) return next(e)
    const refusal = hostEscape((e as { command?: unknown }).command)
    return refusal ? { deny: refusal } : next(e)
  })

  on('ui.close', async ($, e, next) => {
    if (e.id !== PANE) return next(e)
    isPaneOpen = false
    if (e.origin?.kind === 'person') isDismissed = true
    if (isControlled) await takeControl($, false)
    await stopStream($)
    picture = null
    return next(e)
  })

  on('ui.message', async ($, e, next) => {
    if (e.element !== 'takeover') return next(e)
    const data = e.data as { events?: { [key: string]: unknown }[] } | null
    if (data?.events?.length) await writeInput($, data.events)
    return {}
  })

  on('session.end', async ($, e, next) => {
    poller?.cancel()
    poller = null
    await stopStream($)
    return next(e)
  })

  // A toggle at the right of the prompt footer while a realm is live: one
  // press opens the Realm pane, another closes it.
  on('ui.render', { component: 'SessionMode' }, async ($, e, next) => {
    const live = status?.live
    if (!live && !isPaneOpen) return next(e)
    const engine = await next(e)
    const { Box, Button } = $.ui.resolve(e)
    const label = (isPaneOpen ? '◉ ' : '◈ ') + (live ? (live.kind === 'omarchy-vm' ? 'omarchy realm' : 'realm') + (isControlled ? ' · yours' : ' live') : 'realm')
    return (
      <Box flexDirection="row" gap={2}>
        {engine}
        <Button key="realm-toggle" plain label={label} onPress={() => togglePane($)} />
      </Box>
    )
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const live = status?.live
    const header = live ? (
      <Text bold>
        {live.kind === 'omarchy-vm' ? 'Omarchy VM ' : 'Realm '}
        {live.id}
        <Text dimColor>{'  ' + (live.size ?? (live.memory_mb ? live.memory_mb + ' MB' : '')) + (isControlled ? '  · you have control' : '  · agent has control')}</Text>
      </Text>
    ) : (
      <Text bold>Realm</Text>
    )
    const controls = (
      <Box flexDirection="row" gap={1}>
        {live && (
          <Button key="control" hotkey="t" label={isControlled ? 'Hand back' : 'Take control'} variant={isControlled ? 'primary' : undefined} onPress={() => takeControl($, !isControlled)} />
        )}
        {live && <Button key="full" hotkey="f" label="Full view" onPress={() => openFullView($)} />}
        {live && <Button key="stop" hotkey="x" label="Stop" onPress={() => command($, 'stop').then(() => undefined)} />}
        {!live && <Button key="start" hotkey="s" label="Start realm" onPress={() => command($, 'on').then((text) => $.ui.toast(text))} />}
        <Button key="hide" role="dismiss" label="Close" onPress={() => closePane($)} />
      </Box>
    )

    if (!live) {
      const setup = status?.setup
      return (
        <Box flexDirection="column" gap={1}>
          {header}
          <Text>{connectError ? 'The realms server is not connected: ' + connectError : !status ? 'Connecting…' : !status.enabled ? 'Realm use is off for this session.' : setup && !setup.ready ? setup.message : 'No realm is running. It starts when the agent first uses a desktop tool.'}</Text>
          {setup?.install_command && <Text dimColor>{'Run: ! ' + setup.install_command}</Text>}
          {controls}
        </Box>
      )
    }

    if (e.surface !== 'terminal') {
      return (
        <Box flexDirection="column" gap={1}>
          {header}
          <Text>The live picture draws in the terminal. Full view opens it in your browser.</Text>
          {controls}
        </Box>
      )
    }

    const { Image, Raster, Client } = $.ui.resolve(e)
    const viewportRows = e.viewport?.rows ?? 40
    const bodyColumns = (e.props as { bodyColumns?: number }).bodyColumns ?? e.viewport?.columns ?? 80
    const target = mode === 'image' ? fit(bodyColumns, viewportRows, 255, 255) : fit(bodyColumns, viewportRows, 512, 256)
    if (target.columns !== box.columns || target.rows !== box.rows) {
      box = target
      // Raster cells are sized at the source; pixels scale in the terminal.
      if (mode === 'raster' && stream && (streamBox.columns !== box.columns || streamBox.rows !== box.rows)) restartStream($)
    }
    let view
    if (picture?.kind === 'image' && mode === 'image') {
      view = <Image key="view" source={picture.source} columns={box.columns} rows={box.rows} alt="the realm's screen" />
    } else if (picture?.kind === 'raster' && mode === 'raster' && picture.columns === box.columns && picture.rows === box.rows) {
      view = <Raster key="view" columns={picture.columns} rows={picture.rows} cells={picture.cells} />
    } else {
      view = <Text dimColor>{viewError ?? "Connecting to the realm's screen…"}</Text>
    }
    return (
      <Box flexDirection="column">
        {header}
        <Box flexDirection="column">
          {view}
          {isControlled && picture && (
            <Box position="absolute" top={0} left={0}>
              <Client key="takeover" module="./takeover.tsx" width={box.columns} height={box.rows} />
            </Box>
          )}
        </Box>
        <Text dimColor>
          {isControlled
            ? 'Click the picture, then type: keys and clicks go to the realm. Esc returns the keyboard.'
            : mode === 'raster' && inHerdr && imagesRefused
              ? 'Coloured-cell view. herdr can show the sharp picture: start Claude with CLAUDE_CODE_FORCE_TERMINAL_IMAGES=1 (inside herdr on Ghostty or kitty).'
              : mode === 'raster'
              ? 'Coloured-cell view (this terminal draws no pictures; Ghostty or kitty show full pixels). Full view (f) opens it sharp.'
              : 'Live view of the realm. The agent works here, never on your screen.'}
        </Text>
        {controls}
      </Box>
    )
  })
}
