"""Live view of a realm for the Claude Code pane: RFB in, frames out.

Connects to the realm's private VNC socket (wayvnc for a regular realm, QEMU
for an Omarchy VM), keeps a framebuffer, and writes frames for the mod:

``image`` mode
    Each frame is raw RGB in one file (renamed over atomically, see
    ``FrameFile``); stdout carries ``@file <path> <width> <height> <generation>``.
    The terminal reads it itself (kitty graphics protocol: kitty, Ghostty), so
    no pixel passes through Claude Code.

``raster`` mode
    For terminals without image support: the frame downsampled to a grid of
    half-block cells (two pixels per cell), as the base64 cell array a
    ``Raster`` element takes, on ``@raster <columns> <rows> <cells>``.

Input for a person who took control arrives through ``--input``: a small JSON
file the mod rewrites, ``{"events": [{"id", "t": "key"|"ptr", ...}]}``; events
with a new ``id`` are forwarded as RFB KeyEvent / PointerEvent.

Only the standard library: this runs beside the pane at frame rate.
"""

import argparse
from array import array
import base64
import json
import os
from pathlib import Path
import select
import socket
import struct
import sys
import time

# 32 bpp, depth 24, little endian, true colour, red<<16 | green<<8 | blue:
# each pixel read as a native u32 is 0x00RRGGBB (what Raster cells take), and
# its bytes are B, G, R, X.
PIXEL_FORMAT = struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
ENC_RAW, ENC_COPYRECT, ENC_DESKTOP_SIZE = 0, 1, -223
HALF_BLOCK = 0x2580

KEYSYMS = {
    "return": 0xFF0D, "enter": 0xFF0D, "tab": 0xFF09, "backspace": 0xFF08, "delete": 0xFFFF,
    "escape": 0xFF1B, "up": 0xFF52, "down": 0xFF54, "left": 0xFF51, "right": 0xFF53,
    "home": 0xFF50, "end": 0xFF57, "pageup": 0xFF55, "pagedown": 0xFF56, "insert": 0xFF63,
    "space": 0x20,
}
KEYSYMS.update({f"f{n}": 0xFFBE + n - 1 for n in range(1, 13)})
MODIFIERS = (("ctrl", 0xFFE3), ("shift", 0xFFE1), ("meta", 0xFFE9), ("alt", 0xFFE9))
BUTTONS = {"left": 1, "middle": 2, "right": 4}


def keysym(name):
    lowered = name.lower()
    if lowered in KEYSYMS and (len(name) > 1 or name == " "):
        return KEYSYMS[lowered]
    if len(name) == 1:
        code = ord(name)
        return code if code < 0x100 else 0x01000000 + code
    return None


class RFBError(RuntimeError):
    pass


class RFB:
    """Minimal RFB 3.8 client: None security, raw + copyrect + desktop size."""

    def __init__(self, path, *, timeout=10):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(path)
        self.buffer = bytearray()
        self._handshake()
        self.sock.settimeout(None)
        # Nothing worth showing until the server's first update arrives.
        self.dirty = False
        self.mask = 0

    def _read(self, size):
        while len(self.buffer) < size:
            chunk = self.sock.recv(max(65536, size - len(self.buffer)))
            if not chunk:
                raise RFBError("VNC server closed the connection")
            self.buffer.extend(chunk)
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data

    def _handshake(self):
        version = self._read(12)
        if not version.startswith(b"RFB 003."):
            raise RFBError("not an RFB server")
        self.sock.sendall(b"RFB 003.008\n")
        count = self._read(1)[0]
        if count == 0:
            reason = self._read(struct.unpack(">I", self._read(4))[0])
            raise RFBError("VNC refused: " + reason.decode("utf-8", "replace"))
        types = self._read(count)
        if 1 not in types:
            raise RFBError("VNC server requires authentication this viewer does not offer")
        self.sock.sendall(b"\x01")
        if struct.unpack(">I", self._read(4))[0] != 0:
            raise RFBError("VNC security handshake failed")
        self.sock.sendall(b"\x01")  # shared
        width, height = struct.unpack(">HH", self._read(4))
        self._read(16)
        self.name = self._read(struct.unpack(">I", self._read(4))[0]).decode("utf-8", "replace")
        self.sock.sendall(b"\x00\x00\x00\x00" + PIXEL_FORMAT)
        encodings = (ENC_RAW, ENC_COPYRECT, ENC_DESKTOP_SIZE)
        self.sock.sendall(struct.pack(">BxH", 2, len(encodings)) + struct.pack(">" + "i" * len(encodings), *encodings))
        self._resize(width, height)

    def _resize(self, width, height):
        self.width, self.height = width, height
        self.fb = bytearray(width * height * 4)
        self.dirty = True

    def request(self, incremental=True):
        self.sock.sendall(struct.pack(">BBHHHH", 3, 1 if incremental else 0, 0, 0, self.width, self.height))

    def fileno(self):
        return self.sock.fileno()

    def pump(self):
        """Read one server message (blocking); True after a framebuffer update."""
        kind = self._read(1)[0]
        if kind == 0:
            self._read(1)
            (count,) = struct.unpack(">H", self._read(2))
            for _ in range(count):
                x, y, w, h, encoding = struct.unpack(">HHHHi", self._read(12))
                if encoding == ENC_RAW:
                    data = self._read(w * h * 4)
                    stride, row = self.width * 4, w * 4
                    for line in range(h):
                        start = (y + line) * stride + x * 4
                        self.fb[start:start + row] = data[line * row:(line + 1) * row]
                elif encoding == ENC_COPYRECT:
                    sx, sy = struct.unpack(">HH", self._read(4))
                    stride, row = self.width * 4, w * 4
                    rows = [bytes(self.fb[(sy + line) * stride + sx * 4:(sy + line) * stride + sx * 4 + row])
                            for line in range(h)]
                    for line, data in enumerate(rows):
                        start = (y + line) * stride + x * 4
                        self.fb[start:start + row] = data
                elif encoding == ENC_DESKTOP_SIZE:
                    self._resize(w, h)
                else:
                    raise RFBError("unsupported VNC encoding %d" % encoding)
            self.dirty = True
            return True
        if kind == 1:
            self._read(1)
            _, count = struct.unpack(">HH", self._read(4))
            self._read(count * 6)
        elif kind == 2:
            pass
        elif kind == 3:
            self._read(3)
            self._read(struct.unpack(">I", self._read(4))[0])
        else:
            raise RFBError("unsupported VNC server message %d" % kind)
        return False

    # -------------------------------------------------------------- input

    def key(self, sym, down):
        self.sock.sendall(struct.pack(">BBxxI", 4, 1 if down else 0, sym))

    def pointer(self, x, y, mask):
        x = max(0, min(self.width - 1, int(x)))
        y = max(0, min(self.height - 1, int(y)))
        self.sock.sendall(struct.pack(">BBHH", 5, mask, x, y))


# ------------------------------------------------------------------ frames


def scale_plan(width, height, max_width, max_height):
    """Integer subsampling step keeping the frame within the bounds."""
    step = 1
    while width // step > max_width or height // step > max_height:
        step += 1
    return step, width // step, height // step


def rgb_frame(fb, width, height, step):
    """Subsample the BGRX framebuffer by ``step`` and return packed RGB."""
    out_w, out_h = width // step, height // step
    words = memoryview(fb).cast("I")
    rows = bytearray()
    for y in range(out_h):
        start = y * step * width
        rows += words[start:start + out_w * step:step].tobytes()
    rgb = bytearray(out_w * out_h * 3)
    rgb[0::3] = rows[2::4]
    rgb[1::3] = rows[1::4]
    rgb[2::3] = rows[0::4]
    return rgb, out_w, out_h


# Quarter-block glyphs by which of a cell's 2x2 pixels take the foreground:
# bit 1 top-left, 2 top-right, 4 bottom-left, 8 bottom-right.
QUADRANTS = [0x20, 0x2598, 0x259D, 0x2580, 0x2596, 0x258C, 0x259E, 0x259B,
             0x2597, 0x259A, 0x2590, 0x259C, 0x2584, 0x2599, 0x259F, 0x2588]


def _box_downscale(np, words, out_h, out_w):
    """Area-average the 0x00RRGGBB framebuffer to (out_h, out_w, 3) floats.

    Integer block sums over the largest whole blocks (the few edge pixels left
    over are dropped): no full-size float image is ever made, which keeps a
    1920x1080 frame at a few milliseconds.
    """
    height, width = words.shape
    block_h, block_w = max(1, height // out_h), max(1, width // out_w)
    if height < out_h or width < out_w:
        ys = (np.arange(out_h) * height // out_h)
        xs = (np.arange(out_w) * width // out_w)
        words = words[ys][:, xs]
        block_h = block_w = 1
    else:
        words = words[: out_h * block_h, : out_w * block_w]
    channels = []
    for shift in (16, 8, 0):
        channel = ((words >> shift) & 255).astype(np.uint32)
        summed = channel.reshape(out_h, block_h, out_w, block_w).sum(axis=(1, 3), dtype=np.uint32)
        channels.append(summed)
    return np.stack(channels, axis=-1).astype(np.float32) / float(block_h * block_w)


def raster_cells(fb, width, height, columns, rows):
    """Quarter-block cells: each cell shows 2x2 area-averaged pixels in its best
    two colours, so edges and text shapes survive (twice the detail of half blocks)."""
    try:
        import numpy as np
    except ImportError:
        return _half_block_cells(fb, width, height, columns, rows)
    words = np.frombuffer(fb, dtype="<u4", count=width * height).reshape(height, width)
    small = _box_downscale(np, words, rows * 2, columns * 2)
    # (rows, columns, 4 pixels, 3) in bit order TL, TR, BL, BR
    quad = small.reshape(rows, 2, columns, 2, 3).transpose(0, 2, 1, 3, 4).reshape(rows, columns, 4, 3)
    masks = np.array([[(m >> bit) & 1 for bit in range(4)] for m in range(16)], dtype=np.float32)  # (16, 4)
    fg_count = masks.sum(axis=1)  # (16,)
    bg_count = 4 - fg_count
    fg_sum = np.einsum("mp,rcpk->rcmk", masks, quad)
    bg_sum = quad.sum(axis=2)[:, :, None, :] - fg_sum
    fg_mean = fg_sum / np.maximum(fg_count, 1)[None, None, :, None]
    bg_mean = bg_sum / np.maximum(bg_count, 1)[None, None, :, None]
    # Squared error of a split = total - what the two means explain; the
    # constant total drops out of the argmin.
    explained = ((fg_sum ** 2).sum(axis=3) / np.maximum(fg_count, 1)[None, None, :]
                 + (bg_sum ** 2).sum(axis=3) / np.maximum(bg_count, 1)[None, None, :])
    best = explained.argmax(axis=2)  # (rows, columns)
    pick = lambda array: np.take_along_axis(array, best[:, :, None, None], axis=2)[:, :, 0, :]
    fg = pick(fg_mean).round().clip(0, 255).astype(np.uint32)
    bg = pick(bg_mean).round().clip(0, 255).astype(np.uint32)
    glyph = np.array(QUADRANTS, dtype=np.uint32)[best]
    cells = np.empty((rows, columns, 3), dtype="<u4")
    cells[..., 0] = glyph
    cells[..., 1] = (fg[..., 0] << 16) | (fg[..., 1] << 8) | fg[..., 2]
    cells[..., 2] = (bg[..., 0] << 16) | (bg[..., 1] << 8) | bg[..., 2]
    return base64.b64encode(cells.tobytes()).decode()


def _half_block_cells(fb, width, height, columns, rows):
    """Half-block cells without numpy: top pixel as foreground, bottom as background."""
    words = memoryview(fb).cast("I")
    xs = [min(width - 1, int((c + 0.5) * width / columns)) for c in range(columns)]
    cells = array("I")
    for r in range(rows):
        top = min(height - 1, int((2 * r + 0.5) * height / (2 * rows))) * width
        bottom = min(height - 1, int((2 * r + 1.5) * height / (2 * rows))) * width
        for x in xs:
            cells.append(HALF_BLOCK)
            cells.append(words[top + x] & 0xFFFFFF)
            cells.append(words[bottom + x] & 0xFFFFFF)
    if sys.byteorder != "little":
        cells.byteswap()
    return base64.b64encode(cells.tobytes()).decode()


class FrameFile:
    """The newest frame as one file the terminal reads (kitty ``t=f``).

    Each frame is written to a temporary file and renamed over the frame
    path, so a reader always opens one whole frame, old or new, and a redraw
    of the pane at any moment finds the current one. ``generation`` tells the
    terminal the content under the same path changed. The file lives in the
    private runtime directory (tmpfs, mode 0600) and is removed at exit.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.generation = 0

    def write(self, data):
        self.generation += 1
        temporary = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)
        os.replace(temporary, self.path)
        return self.generation

    def close(self):
        for path in (self.path, self.path.with_name(self.path.name + ".tmp")):
            path.unlink(missing_ok=True)


def default_frame_path():
    base = os.environ.get("XDG_RUNTIME_DIR")
    if not base or not Path(base).is_dir():
        base = "/tmp"
    return Path(base) / ("claude-realms-%d" % os.getuid()) / ("frame-%d.rgb" % os.getpid())


# ------------------------------------------------------------------ input


class InputFile:
    """Events the pane writes while a person holds control.

    The file is ``{"epoch": ..., "events": [{"id": n, ...}]}``, rewritten whole
    with the most recent events. Events present when the viewer starts are
    history; after that every event with a new id is forwarded once. A new
    epoch (a new takeover, or a reloaded pane) starts the ids over.
    """

    def __init__(self, path):
        self.path = Path(path) if path else None
        self.stamp = None
        self.epoch, events = self._read() or (None, [])
        self.last_id = max((e.get("id", 0) for e in events if isinstance(e.get("id"), int)), default=0)

    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8")) if self.path else None
        except (ValueError, OSError):
            return None
        if not isinstance(data, dict):
            return None
        return data.get("epoch"), [e for e in data.get("events", []) if isinstance(e, dict)]

    def poll(self):
        if self.path is None:
            return []
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return []
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self.stamp:
            return []
        read = self._read()
        if read is None:
            return []  # half-written; read again next tick
        self.stamp = stamp
        epoch, events = read
        if epoch != self.epoch:
            self.epoch, self.last_id = epoch, 0
        fresh = [e for e in events if isinstance(e.get("id"), int) and e["id"] > self.last_id]
        if fresh:
            self.last_id = max(e["id"] for e in fresh)
        return fresh


def apply_input(rfb, event):
    if event.get("t") == "key":
        sym = keysym(str(event.get("key", "")))
        if sym is None:
            return
        held = [code for name, code in MODIFIERS if event.get(name)]
        for code in held:
            rfb.key(code, True)
        rfb.key(sym, True)
        rfb.key(sym, False)
        for code in reversed(held):
            rfb.key(code, False)
    elif event.get("t") == "ptr":
        columns, rows = max(1, event.get("columns", 1)), max(1, event.get("rows", 1))
        x = float(event.get("x", 0)) / columns * rfb.width
        y = float(event.get("y", 0)) / rows * rfb.height
        kind = event.get("type")
        button = BUTTONS.get(event.get("button"), 0)
        if kind == "down":
            rfb.mask |= button
        elif kind == "up":
            rfb.mask &= ~button
        elif kind == "wheel":
            bit = 8 if event.get("delta", 0) < 0 else 16
            rfb.pointer(x, y, rfb.mask | bit)
        rfb.pointer(x, y, rfb.mask)


# ------------------------------------------------------------------- main


def main(argv=None):
    parser = argparse.ArgumentParser(description="Stream a realm's screen to the Claude Code pane")
    parser.add_argument("--socket", required=True)
    parser.add_argument("--mode", choices=("image", "raster"), default="image")
    parser.add_argument("--columns", type=int, default=100)
    parser.add_argument("--rows", type=int, default=30)
    parser.add_argument("--fps", type=float, default=10)
    # Native resolution by default: the terminal scales the picture to the
    # pane itself, and a pre-shrunk frame only comes out blurrier.
    parser.add_argument("--max-width", type=int, default=2560)
    parser.add_argument("--max-height", type=int, default=1600)
    parser.add_argument("--input")
    parser.add_argument("--frames", type=int, default=0, help="exit after N frames (tests)")
    parser.add_argument("--frame-file", help="where image frames are written (default: private runtime dir)")
    args = parser.parse_args(argv)

    out = sys.stdout
    try:
        rfb = RFB(args.socket)
    except (OSError, RFBError) as exc:
        print("@error " + str(exc).replace("\n", " "), file=out, flush=True)
        return 1
    # Read the input history before announcing ourselves: whatever the pane
    # writes after @hello is new input, never history.
    inputs = InputFile(args.input)
    print("@hello " + json.dumps({"width": rfb.width, "height": rfb.height, "name": rfb.name}),
          file=out, flush=True)
    frame_file = FrameFile(args.frame_file or default_frame_path())
    interval = 1.0 / max(0.5, min(args.fps, 30))
    last_emit = 0.0
    sent = 0
    pending = False
    size = (rfb.width, rfb.height)
    try:
        rfb.request(incremental=False)
        pending = True
        while True:
            now = time.monotonic()
            wait = max(0.0, min(0.05, last_emit + interval - now)) if rfb.dirty else 0.05
            readable, _, _ = select.select([rfb], [], [], wait)
            if readable or rfb.buffer:
                if rfb.pump():
                    pending = False
            for event in inputs.poll():
                apply_input(rfb, event)
            now = time.monotonic()
            if (rfb.width, rfb.height) != size:
                size = (rfb.width, rfb.height)
                print("@size %d %d" % size, file=out, flush=True)
            if rfb.dirty and now - last_emit >= interval:
                rfb.dirty = False
                last_emit = now
                if args.mode == "image":
                    step, w, h = scale_plan(rfb.width, rfb.height, args.max_width, args.max_height)
                    data, w, h = rgb_frame(rfb.fb, rfb.width, rfb.height, step)
                    generation = frame_file.write(data)
                    print("@file %s %d %d %d" % (frame_file.path, w, h, generation), file=out, flush=True)
                else:
                    cells = raster_cells(rfb.fb, rfb.width, rfb.height, args.columns, args.rows)
                    print("@raster %d %d %s" % (args.columns, args.rows, cells), file=out, flush=True)
                sent += 1
                if args.frames and sent >= args.frames:
                    return 0
            if not pending and now - last_emit >= interval * 0.5:
                rfb.request(incremental=True)
                pending = True
    except (OSError, RFBError, BrokenPipeError) as exc:
        try:
            print("@error " + str(exc).replace("\n", " "), file=out, flush=True)
        except BrokenPipeError:
            pass
        return 1
    finally:
        frame_file.close()


if __name__ == "__main__":
    raise SystemExit(main())
