"""Render a tmux capture (with -e escapes) to a PNG: each cell 6x12 pixels,
half-block cells as two colour blocks, text cells as their background with a
mid-grey bar for the glyph. Enough to see what the Realm pane shows."""
import re
import struct
import sys
import zlib

CELL_W, CELL_H = 6, 12
PALETTE = {}
QUADRANTS = {0x2598: 1, 0x259D: 2, 0x2580: 3, 0x2596: 4, 0x258C: 5, 0x259E: 6, 0x259B: 7,
             0x2597: 8, 0x259A: 9, 0x2590: 10, 0x259C: 11, 0x2584: 12, 0x2599: 13, 0x259F: 14, 0x2588: 15}


def xterm(n):
    if n < 16:
        base = [(0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0), (0, 0, 238), (205, 0, 205), (0, 205, 205), (229, 229, 229),
                (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0), (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255)]
        return base[n]
    if n < 232:
        n -= 16
        steps = [0, 95, 135, 175, 215, 255]
        return steps[n // 36], steps[(n // 6) % 6], steps[n % 6]
    v = 8 + (n - 232) * 10
    return v, v, v


def parse(text):
    rows = []
    for line in text.split("\n"):
        fg, bg = (200, 200, 200), (20, 20, 20)
        cells = []
        for token in re.split(r"(\x1b\[[0-9;:]*m)", line):
            if token.startswith("\x1b["):
                codes = [int(c) if c else 0 for c in token[2:-1].replace(":", ";").split(";")]
                i = 0
                while i < len(codes):
                    c = codes[i]
                    if c == 0:
                        fg, bg = (200, 200, 200), (20, 20, 20)
                    elif c in (38, 48) and i + 1 < len(codes):
                        if codes[i + 1] == 2:
                            colour = tuple(codes[i + 2:i + 5]); i += 4
                        else:
                            colour = xterm(codes[i + 2]); i += 2
                        if c == 38:
                            fg = colour
                        else:
                            bg = colour
                    elif c == 39:
                        fg = (200, 200, 200)
                    elif c == 49:
                        bg = (20, 20, 20)
                    i += 1
                continue
            for ch in token:
                cells.append((ch, fg, bg))
        rows.append(cells)
    return rows


def render(rows, path):
    width = max(len(r) for r in rows) * CELL_W
    height = len(rows) * CELL_H
    pixels = [bytearray([20, 20, 20] * width) for _ in range(height)]
    for y, row in enumerate(rows):
        for x, (ch, fg, bg) in enumerate(row):
            mask = QUADRANTS.get(ord(ch))
            for dy in range(CELL_H):
                line = pixels[y * CELL_H + dy]
                for dx in range(CELL_W):
                    if mask is not None:
                        bit = (1 if dx < CELL_W // 2 else 2) * (1 if dy < CELL_H // 2 else 4)
                        colour = fg if mask & bit else bg
                    elif ch.strip() and 3 <= dy <= 8:
                        colour = tuple((a + b) // 2 for a, b in zip(fg, bg))
                    else:
                        colour = bg
                    o = (x * CELL_W + dx) * 3
                    line[o:o + 3] = bytes(colour)
    raw = b"".join(b"\x00" + bytes(line) for line in pixels)
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) \
        + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")
    open(path, "wb").write(png)


if __name__ == "__main__":
    render(parse(open(sys.argv[1], encoding="utf-8", errors="replace").read()), sys.argv[2])
