// Laid over the live realm picture while the person holds control: forwards
// what they type and click to the realm's desktop. Keys arrive as presses (the
// terminal reports no releases); pointer positions arrive in cells, with the
// sub-cell fraction where the terminal reports pixels.

import type { ClientModule, ClientPointerEvent, JsonValue } from 'claude-code'

type Event = { [key: string]: JsonValue }
type State = { queue: Event[] }

const Takeover: ClientModule<JsonValue, State> = (_props, surface) => {
  if (surface.state === undefined) {
    const state: State = { queue: [] }
    surface.onKey((e) => {
      const event: Event = { t: 'key', key: e.key }
      if (e.ctrl) event.ctrl = true
      if (e.shift) event.shift = true
      if (e.meta) event.meta = true
      state.queue.push(event)
    })
    surface.onPointer((e: ClientPointerEvent) => {
      if (e.type === 'enter' || e.type === 'leave') return
      // A hover move without a button carries no information for the desktop
      // worth a post per frame; drags (move with a button) do.
      if (e.type === 'move' && !e.button) return
      state.queue.push({
        t: 'ptr',
        type: e.type,
        button: e.button ?? 'left',
        x: e.fine?.x ?? e.x + 0.5,
        y: e.fine?.y ?? e.y + 0.5,
        columns: surface.columns,
        rows: surface.rows,
      })
    })
    // One post per frame at most: send everything queued since the last one.
    surface.every(30, () => {
      if (state.queue.length === 0) return
      const events = state.queue.splice(0)
      surface.post({ events })
    })
    surface.setState(state)
  }
  const { Box } = surface.elements
  return <Box width="100%" height="100%" />
}

export default Takeover
