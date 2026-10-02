import { expect, mock, test } from 'claude-code/testing'

// The realms MCP server as the mod sees it through $.mcp, a realm that goes
// live when started, and the frames the live-view process prints.
const LIVE = {
  id: 'r-0123456789abcdef01234567',
  kind: 'realm',
  size: '1920x1080',
  vnc_socket: '/run/user/1000/cr-0123456789abcdef/wayvnc.sock',
}
const PANE_PROPS = {
  title: 'Realm',
  isFocused: false,
  bodyColumns: 100,
  placement: 'dock',
  scroll: { top: 0 },
  view: {},
} as never

function statusOf(live: typeof LIVE | null, controlled = false) {
  return {
    enabled: true,
    kind: 'realm',
    live,
    setup: { ready: true, message: 'Ready.', steps: [], install_command: null },
    frames_argv: ['/data/venv/bin/python', '-P', '-m', 'claude_realms.frames'],
    input_path: '/data/realms/input/abc.json',
    controlled,
    jobs: {},
    events: [],
  }
}

type Options = { live?: boolean; placed?: boolean; frames?: string[]; denyBlit?: string; codemux?: boolean }

function stubClaudeCode(on: any, clock: any, { live = false, placed = true, frames = [], denyBlit, codemux = false }: Options = {}) {
  const seen = {
    realm: [] as any[],
    opens: 0,
    closes: 0,
    toasts: [] as string[],
    spawns: [] as string[][],
    blits: [] as any[],
    writes: [] as { path: string; text: string }[],
    status: [] as (string | undefined)[],
    toolCalls: [] as any[],
    opened: [] as string[],
  }
  let current: typeof LIVE | null = live ? LIVE : null
  let controlled = false
  const CONTROL = {
    pid: 77,
    socket: '/run/user/1000/claude-realms-1000/77.sock',
    python: '/data/venv/bin/python',
    client: '/plugin/src/claude_realms/ctl.py',
    ancestors: [{ pid: 77 }, { pid: 76 }, { pid: 4242 }],
  }
  // The person's actions go over the server's control socket: the mod finds
  // the server below its own Claude Code process (PID 4242) and runs ctl.py.
  on('process.run', ($: any, e: any) => {
    if (e.argv[0] === '/bin/sh') return { value: { exitCode: 0, stdout: '4242 1000\n', stderr: '' } }
    if (e.argv[0] === 'xdg-open') {
      seen.opened.push(e.argv[1])
      return { value: { exitCode: 0, stdout: '', stderr: '' } }
    }
    if (e.argv[0] === 'codemux') {
      seen.opened.push(e.argv.join(' '))
      return { value: { exitCode: 0, stdout: '', stderr: '' } }
    }
    expect(e.argv.slice(0, 5)).toEqual([CONTROL.python, '-I', '-S', CONTROL.client, CONTROL.socket])
    const args = JSON.parse(e.argv[5])
    seen.realm.push(args)
    if (args.action === 'on') current = LIVE
    if (args.action === 'stop' || args.action === 'off') current = null
    if (args.action === 'control') controlled = Boolean(args.control)
    const text = current ? 'realm ' + current.id + ' is live' : 'No realm running'
    const data = args.action === 'watch' ? { url: 'http://127.0.0.1:5555/realms/x/view#ticket=t' } : statusOf(current, controlled)
    const reply = { texts: [text, JSON.stringify(data)], isError: false }
    return { value: { exitCode: 0, stdout: JSON.stringify(reply) + '\n', stderr: '' } }
  })
  on('env.get', ($: any, e: any) => ({
    value: e.name === 'XDG_RUNTIME_DIR' ? '/run/user/1000' : e.name === 'CODEMUX' && codemux ? '1' : undefined,
  }))
  on('fs.exists', () => ({ value: true }))
  on('fs.list', () => ({ value: [{ name: '77.json', kind: 'file', size: 10, mtimeMs: 0, isLink: false }, { name: '77.sock', kind: 'other', size: 0, mtimeMs: 0, isLink: false }] }))
  on('fs.read', ($: any, e: any) => ({ value: e.path.endsWith('77.json') ? JSON.stringify(CONTROL) : '' }))
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: '/work' }))
  on('ui.open', () => {
    seen.opens += 1
    return { value: placed ? { isPlaced: true } : { isPlaced: false, reason: 'narrow' } }
  })
  on('ui.close', () => {
    seen.closes += 1
    return { value: undefined }
  })
  on('ui.render', () => ({ type: 'Text', props: {}, children: [''] }))
  on('ui.toast', ($: any, e: any) => {
    seen.toasts.push(e.text)
    return { value: undefined }
  })
  on('ui.status', ($: any, e: any) => {
    seen.status.push(e.text)
    return { value: undefined }
  })
  on('ui.log', () => ({ value: undefined }))
  on('ui.focus', () => ({ value: {} }))
  on('ui.copy', () => ({ value: { isCopied: true } }))
  on('ui.blit', ($: any, e: any) => {
    seen.blits.push(e)
    return { value: denyBlit ? { deny: denyBlit } : {} }
  })
  on('fs.write', ($: any, e: any) => {
    seen.writes.push(e)
    return { value: undefined }
  })
  on('process.spawn', async function* ($: any, e: any) {
    seen.spawns.push(e.argv)
    for (const text of frames) yield { stream: 'stdout', text }
    await clock.sleep(60 * 60 * 1000)
    return { code: 0, signal: null }
  })
  // What the agent's realm tools answer once the mod lets them through
  on('tool.call', ($: any, e: any) => {
    seen.toolCalls.push(e)
    if (String(e.tool).includes('realm_launch')) current = LIVE
    return { result: 'ok' }
  })
  return seen
}

async function startSession($: any) {
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
}

const realm = ($: any, args: string) => $.command.run({ command: 'realm', args })

test('/realm status reports the server', async ($, on) => {
  const clock = mock.clock(on)
  const seen = stubClaudeCode(on, clock, { live: true })
  await startSession($)
  const result: any = await realm($, 'status')
  expect(result.text).toContain('is live')
  expect(seen.realm.some((args) => args.action === 'status')).toBe(true)
  const footer: any = await $.ui.mount({ plugin: 'realms', surface: 'terminal', component: 'SessionMode', props: { modes: [] } as never } as never)
  expect((await footer.find({ key: 'realm-toggle' } as never))?.text).toContain('realm live')
})

test('the footer toggle opens and closes the Realm pane', async ($, on) => {
  const clock = mock.clock(on)
  const seen = stubClaudeCode(on, clock, { live: true })
  await startSession($)
  await realm($, 'status')
  const footer: any = await $.ui.mount({ plugin: 'realms', surface: 'terminal', component: 'SessionMode', props: { modes: ['focus'] } as never } as never)
  await footer.press({ key: 'realm-toggle' })
  expect(seen.opens).toBe(1)
  await footer.press({ key: 'realm-toggle' })
  expect(seen.closes).toBe(1)
})

test('no toggle in the footer while no realm is live', async ($, on) => {
  const clock = mock.clock(on)
  stubClaudeCode(on, clock, { live: false })
  await startSession($)
  await realm($, 'status')
  const footer: any = await $.ui.mount({ plugin: 'realms', surface: 'terminal', component: 'SessionMode', props: { modes: [] } as never } as never)
  expect(await footer.find({ key: 'realm-toggle' } as never)).toBeUndefined()
})

test('full view opens the realm in the browser with control', async ($, on) => {
  const clock = mock.clock(on)
  const seen = stubClaudeCode(on, clock, { live: true })
  await startSession($)
  await realm($, 'full')
  expect(seen.realm.some((args) => args.action === 'watch' && args.control === true)).toBe(true)
  expect(seen.opened).toEqual(['http://127.0.0.1:5555/realms/x/view#ticket=t'])
})


test('the guard refuses Bash that points GUI tools at the host while a realm is live', async ($, on) => {
  const clock = mock.clock(on)
  stubClaudeCode(on, clock, { live: true })
  await startSession($)
  await realm($, 'status')
  const escaped: any = await $.tool.call({ tool: 'Bash', tool_use_id: 'u1', command: 'DISPLAY=:0 xdotool click 1' } as never)
  expect(JSON.stringify(escaped)).toContain('Blocked')
  const ordinary: any = await $.tool.call({ tool: 'Bash', tool_use_id: 'u2', command: 'ls -la && echo DISPLAY=:0' } as never)
  expect(JSON.stringify(ordinary)).not.toContain('Blocked')
})

test('the guard stays out of the way while no realm is live', async ($, on) => {
  const clock = mock.clock(on)
  stubClaudeCode(on, clock, { live: false })
  await startSession($)
  await realm($, 'status')
  const result: any = await $.tool.call({ tool: 'Bash', tool_use_id: 'u1', command: 'WAYLAND_DISPLAY=wayland-1 foot' } as never)
  expect(JSON.stringify(result)).not.toContain('Blocked')
})

test('the pane opens when the agent first brings a realm up', async ($, on) => {
  const clock = mock.clock(on)
  const seen = stubClaudeCode(on, clock)
  await startSession($)
  await realm($, 'status')
  expect(seen.opens).toBe(0)
  await $.tool.call({ tool: 'mcp__plugin_realms_realms__realm_launch', tool_use_id: 'u1', command: 'gtk3-demo' } as never)
  await clock.advance(10)
  expect(seen.opens).toBe(1)
})

test('a narrow terminal gets a toast instead of a pane that pops up later', async ($, on) => {
  const clock = mock.clock(on)
  const seen = stubClaudeCode(on, clock, { placed: false })
  await startSession($)
  await realm($, 'status')
  await $.tool.call({ tool: 'mcp__plugin_realms_realms__realm_launch', tool_use_id: 'u1', command: 'gtk3-demo' } as never)
  await clock.advance(10)
  expect(seen.closes).toBe(1)
  expect(seen.toasts.join(' ')).toContain('/realm view')
})

test('the live view streams pixels, then blits each new frame', async ($, on) => {
  const clock = mock.clock(on)
  const seen = stubClaudeCode(on, clock, {
    live: true,
    frames: ['@hello {"width":1920,"height":1080,"name":"WayVNC"}\n', '@file /run/user/1000/claude-realms-1000/frame-1.rgb 1920 1080 1\n@file /run/user/1000/claude-realms-1000/frame-1.rgb 1920 1080 2\n'],
  })
  await startSession($)
  await realm($, 'view')
  const ui = await $.ui.mount({ plugin: 'realms', surface: 'terminal', component: 'Pane', requestId: 'realm', props: PANE_PROPS, viewport: { columns: 200, rows: 60 } } as never)
  await clock.advance(10)
  expect(seen.spawns[0]).toContain('--socket')
  expect(seen.spawns[0]).toContain(LIVE.vnc_socket)
  expect(seen.spawns[0]).toContain('image')
  const view: any = await ui.find({ key: 'view' } as never)
  expect(view?.type).toBe('Image')
  expect(seen.blits.at(-1)?.source?.generation).toBe(2)
})

test('a terminal without pictures falls back to coloured cells', async ($, on) => {
  const clock = mock.clock(on)
  const seen = stubClaudeCode(on, clock, {
    live: true,
    denyBlit: 'the Image draws its alt here: the terminal draws no placeholder images (probe: terminal xterm.js)',
    frames: ['@hello {"width":1920,"height":1080,"name":"WayVNC"}\n', '@file /run/user/1000/claude-realms-1000/frame-1.rgb 1920 1080 1\n@file /run/user/1000/claude-realms-1000/frame-1.rgb 1920 1080 2\n'],
  })
  await startSession($)
  await realm($, 'view')
  await $.ui.mount({ plugin: 'realms', surface: 'terminal', component: 'Pane', requestId: 'realm', props: PANE_PROPS, viewport: { columns: 200, rows: 60 } } as never)
  await clock.advance(200)
  expect(seen.spawns.length).toBeGreaterThan(1)
  expect(seen.spawns.at(-1)).toContain('raster')
})

test('taking control pauses the agent and forwards keys to the realm', async ($, on) => {
  const clock = mock.clock(on)
  const seen = stubClaudeCode(on, clock, {
    live: true,
    frames: ['@hello {"width":1920,"height":1080,"name":"WayVNC"}\n', '@file /run/user/1000/claude-realms-1000/frame-1.rgb 1920 1080 1\n'],
  })
  await startSession($)
  await realm($, 'view')
  const ui: any = await $.ui.mount({ plugin: 'realms', surface: 'terminal', component: 'Pane', requestId: 'realm', props: PANE_PROPS, viewport: { columns: 200, rows: 60 } } as never)
  await clock.advance(10)
  await ui.press({ key: 'control' })
  expect(seen.realm.some((args) => args.action === 'control' && args.control === true)).toBe(true)
  await ui.post({ events: [{ t: 'key', key: 'a' }] }, { in: 'takeover' })
  const written = seen.writes.at(-1)
  expect(written?.path).toBe('/data/realms/input/abc.json')
  const file = JSON.parse(written?.text ?? '{}')
  expect(file.events[0]).toMatchObject({ t: 'key', key: 'a', id: 1 })
  expect(typeof file.epoch).toBe('string')
})

test('closing the pane stops the live view', async ($, on) => {
  const clock = mock.clock(on)
  const seen = stubClaudeCode(on, clock, {
    live: true,
    frames: ['@hello {"width":1920,"height":1080,"name":"WayVNC"}\n', '@file /run/user/1000/claude-realms-1000/frame-1.rgb 1920 1080 1\n'],
  })
  await startSession($)
  await realm($, 'view')
  const ui: any = await $.ui.mount({ plugin: 'realms', surface: 'terminal', component: 'Pane', requestId: 'realm', props: PANE_PROPS, viewport: { columns: 200, rows: 60 } } as never)
  await clock.advance(10)
  expect(seen.spawns.length).toBe(1)
  await ui.press({ key: 'hide' })
  expect(seen.closes).toBe(1)
  await clock.advance(5000)
  // No new viewer is started for a closed pane, however often status refreshes
  expect(seen.spawns.length).toBe(1)
})

test('the desktop app gets status and a browser link instead of a picture', async ($, on) => {
  const clock = mock.clock(on)
  stubClaudeCode(on, clock, { live: true })
  await startSession($)
  await realm($, 'view')
  const ui: any = await $.ui.mount({ plugin: 'realms', surface: 'desktop', component: 'Pane', requestId: 'realm', props: PANE_PROPS, viewport: { columns: 200, rows: 60 } } as never)
  expect(await ui.find({ key: 'view' } as never)).toBeUndefined()
  expect(await ui.find({ key: 'full' } as never)).toBeDefined()
})

test("a subagent's realm calls carry its id to the server", async ($, on) => {
  const clock = mock.clock(on)
  const seen = stubClaudeCode(on, clock, { live: true })
  await startSession($)
  await $.tool.call({ tool: 'mcp__plugin_realms_realms__realm', tool_use_id: 'u1', action: 'on', separate: true, agentId: 'agent-7' } as never)
  await $.tool.call({ tool: 'mcp__plugin_realms_realms__click', tool_use_id: 'u2', x: 1, y: 2 } as never)
  expect(seen.toolCalls[0]._agent).toBe('agent-7')
  expect(seen.toolCalls[1]._agent).toBeUndefined()
})

test('inside Codemux the full view opens in a Codemux browser pane', async ($, on) => {
  const clock = mock.clock(on)
  const seen = stubClaudeCode(on, clock, { live: true, codemux: true })
  await startSession($)
  await realm($, 'full')
  expect(seen.opened).toEqual(['codemux browser open http://127.0.0.1:5555/realms/x/view#ticket=t'])
})

test('in Codemux the realm opens in a browser pane, not the cell pane', async ($, on) => {
  const clock = mock.clock(on)
  const seen = stubClaudeCode(on, clock, { codemux: true })
  await startSession($)
  await realm($, 'status')
  await $.tool.call({ tool: 'mcp__plugin_realms_realms__realm_launch', tool_use_id: 'u1', command: 'chromium' } as never)
  await clock.advance(10)
  expect(seen.opens).toBe(0)
  expect(seen.opened).toEqual(['codemux browser open http://127.0.0.1:5555/realms/x/view#ticket=t'])
  // Once per realm: more agent calls do not open it again.
  await $.tool.call({ tool: 'mcp__plugin_realms_realms__click', tool_use_id: 'u2', x: 1, y: 1 } as never)
  await clock.advance(10)
  expect(seen.opened.length).toBe(1)
  // The footer toggle brings it back.
  const footer: any = await $.ui.mount({ plugin: 'realms', surface: 'terminal', component: 'SessionMode', props: { modes: [] } as never } as never)
  await footer.press({ key: 'realm-toggle' })
  expect(seen.opened.length).toBe(2)
})

test('a transient refusal does not give up on pictures', async ($, on) => {
  const clock = mock.clock(on)
  const seen = stubClaudeCode(on, clock, {
    live: true,
    denyBlit: 'the surface has not written its last frames (paused or busy); blit again later',
    frames: ['@hello {"width":1920,"height":1080,"name":"WayVNC"}\n', '@file /f.rgb 1920 1080 1\n@file /f.rgb 1920 1080 2\n'],
  })
  await startSession($)
  await realm($, 'view')
  await $.ui.mount({ plugin: 'realms', surface: 'terminal', component: 'Pane', requestId: 'realm', props: PANE_PROPS, viewport: { columns: 200, rows: 60 } } as never)
  await clock.advance(200)
  expect(seen.spawns.length).toBe(1)
  expect(seen.spawns[0]).toContain('image')
})
