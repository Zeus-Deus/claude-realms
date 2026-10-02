"""The live view against a real RFB peer on a Unix socket."""
import base64
from array import array
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import threading

import pytest

from claude_realms import frames


class FakeVNC:
    """Speaks just enough RFB 3.8 server: None security, raw rects, input capture."""

    def __init__(self, path, width=8, height=4, colour=0x00FF8000):
        self.path, self.width, self.height, self.colour = str(path), width, height, colour
        self.client_messages = []
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(self.path)
        self.listener.listen(1)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def recv(self, peer, size):
        data = b""
        while len(data) < size:
            chunk = peer.recv(size - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    def serve(self):
        peer, _ = self.listener.accept()
        with peer:
            peer.sendall(b"RFB 003.008\n")
            self.recv(peer, 12)
            peer.sendall(b"\x01\x01")
            assert self.recv(peer, 1) == b"\x01"
            peer.sendall(struct.pack(">I", 0))
            self.recv(peer, 1)
            name = b"fake"
            peer.sendall(struct.pack(">HH", self.width, self.height) + b"\x00" * 16
                         + struct.pack(">I", len(name)) + name)
            try:
                while True:
                    kind = self.recv(peer, 1)[0]
                    if kind == 0:
                        self.recv(peer, 19)
                    elif kind == 2:
                        self.recv(peer, 1)
                        (count,) = struct.unpack(">H", self.recv(peer, 2))
                        self.recv(peer, 4 * count)
                    elif kind == 3:
                        self.recv(peer, 9)
                        pixels = struct.pack("<I", self.colour) * (self.width * self.height)
                        peer.sendall(struct.pack(">BxH", 0, 1)
                                     + struct.pack(">HHHHi", 0, 0, self.width, self.height, 0) + pixels)
                    elif kind == 4:
                        self.client_messages.append(("key",) + struct.unpack(">BxxI", self.recv(peer, 7)))
                    elif kind == 5:
                        self.client_messages.append(("ptr",) + struct.unpack(">BHH", self.recv(peer, 5)))
                    else:
                        return
            except (EOFError, OSError):
                return


@pytest.fixture
def vnc(tmp_path):
    return FakeVNC(tmp_path / "vnc.sock")


def run_frames(vnc, *args, input_path=None, frames_count=2):
    command = [sys.executable, "-m", "claude_realms.frames", "--socket", vnc.path, "--frames", str(frames_count),
               "--fps", "30", *args]
    if input_path:
        command += ["--input", str(input_path)]
    return subprocess.run(command, capture_output=True, text=True, timeout=20)


def test_image_frames_are_one_atomically_replaced_file(vnc, tmp_path):
    frame = tmp_path / "frame.rgb"
    result = run_frames(vnc, "--mode", "image", "--frame-file", str(frame), frames_count=1)
    lines = result.stdout.splitlines()
    assert json.loads(lines[0].removeprefix("@hello ")) == {"width": 8, "height": 4, "name": "fake"}
    path, width, height, generation = lines[1].removeprefix("@file ").split()
    assert (path, int(width), int(height), int(generation)) == (str(frame), 8, 4, 1)
    # The viewer removes its frame file when it exits.
    assert not frame.exists()


def test_raster_cells_carry_the_realm_colours(vnc):
    result = run_frames(vnc, "--mode", "raster", "--columns", "4", "--rows", "2", frames_count=1)
    line = next(line for line in result.stdout.splitlines() if line.startswith("@raster"))
    _, columns, rows, cells = line.split()
    words = array("I")
    words.frombytes(base64.b64decode(cells))
    assert (int(columns), int(rows), len(words)) == (4, 2, 4 * 2 * 3)
    # A solid area is one colour: a blank cell on that background.
    glyph, _, background = words[:3]
    assert glyph == 0x20 and background == 0xFF8000


def test_quadrant_cells_keep_shapes_inside_a_cell():
    import numpy as np
    # Black left column, white right column: the left-half block, black on white.
    image = np.zeros((2, 2), dtype="<u4")
    image[:, 1] = 0xFFFFFF
    words = array("I")
    words.frombytes(base64.b64decode(frames.raster_cells(bytearray(image.tobytes()), 2, 2, 1, 1)))
    assert list(words) == [0x258C, 0x000000, 0xFFFFFF]
    # A diagonal: the matching diagonal glyph.
    image = np.zeros((2, 2), dtype="<u4")
    image[0, 0] = image[1, 1] = 0xFFFFFF
    words = array("I")
    words.frombytes(base64.b64decode(frames.raster_cells(bytearray(image.tobytes()), 2, 2, 1, 1)))
    assert words[0] in (0x259A, 0x259E) and {words[1], words[2]} == {0x000000, 0xFFFFFF}


def test_rgb_frame_subsamples_and_reorders():
    fb = bytearray()
    for y in range(4):
        for x in range(4):
            fb += struct.pack("<I", (x * 16) << 16 | (y * 16) << 8 | 7)
    rgb, width, height = frames.rgb_frame(fb, 4, 4, 2)
    assert (width, height) == (2, 2)
    assert rgb[:3] == bytes([0, 0, 7]) and rgb[3:6] == bytes([32, 0, 7]) and rgb[6:9] == bytes([0, 32, 7])
    assert frames.scale_plan(1920, 1080, 1280, 800) == (2, 960, 540)


def test_keysyms_cover_names_letters_and_unicode():
    assert frames.keysym("return") == 0xFF0D
    assert frames.keysym("a") == ord("a")
    assert frames.keysym("A") == ord("A")
    assert frames.keysym(" ") == 0x20
    assert frames.keysym("€") == 0x01000000 + ord("€")
    assert frames.keysym("f5") == 0xFFC2
    assert frames.keysym("nonsense-key") is None


def test_input_file_forwards_only_new_events(vnc, tmp_path):
    path = tmp_path / "input.json"
    path.write_text(json.dumps({"epoch": "a", "events": [{"id": 1, "t": "key", "key": "x"}]}))
    process = subprocess.Popen([sys.executable, "-m", "claude_realms.frames", "--socket", vnc.path, "--mode", "raster",
                                "--columns", "4", "--rows", "2", "--fps", "30", "--input", str(path)],
                               stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().startswith("@hello")
        assert process.stdout.readline().startswith("@raster")
        events = [{"id": 1, "t": "key", "key": "x"},
                  {"id": 2, "t": "key", "key": "c", "ctrl": True},
                  {"id": 3, "t": "ptr", "type": "down", "button": "left", "x": 2, "y": 1, "columns": 4, "rows": 2}]
        os.utime(path, None)
        path.write_text(json.dumps({"epoch": "a", "events": events}))
        deadline = __import__("time").monotonic() + 5
        while len(vnc.client_messages) < 5 and __import__("time").monotonic() < deadline:
            __import__("time").sleep(0.05)
    finally:
        process.terminate()
        process.wait(timeout=5)
    keys = [m for m in vnc.client_messages if m[0] == "key"]
    # The history event (id 1) predates the viewer and is not replayed.
    assert keys == [("key", 1, 0xFFE3), ("key", 1, ord("c")), ("key", 0, ord("c")), ("key", 0, 0xFFE3)]
    pointer = [m for m in vnc.client_messages if m[0] == "ptr"]
    assert pointer[0] == ("ptr", 1, 4, 2)


def test_first_takeover_events_are_not_mistaken_for_history(vnc, tmp_path):
    # No input file when the viewer starts: the first events written are new.
    path = tmp_path / "input.json"
    process = subprocess.Popen([sys.executable, "-m", "claude_realms.frames", "--socket", vnc.path, "--mode", "raster",
                                "--columns", "4", "--rows", "2", "--fps", "30", "--input", str(path)],
                               stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().startswith("@hello")
        path.write_text(json.dumps({"epoch": "t1", "events": [{"id": 1, "t": "key", "key": "q"}]}))
        import time
        deadline = time.monotonic() + 5
        while not any(m[0] == "key" for m in vnc.client_messages) and time.monotonic() < deadline:
            time.sleep(0.05)
        # A new takeover restarts ids at 1 under a new epoch.
        path.write_text(json.dumps({"epoch": "t2", "events": [{"id": 1, "t": "key", "key": "w"}]}))
        while sum(m[0] == "key" for m in vnc.client_messages) < 4 and time.monotonic() < deadline + 5:
            time.sleep(0.05)
    finally:
        process.terminate()
        process.wait(timeout=5)
    keys = [m for m in vnc.client_messages if m[0] == "key"]
    assert keys == [("key", 1, ord("q")), ("key", 0, ord("q")), ("key", 1, ord("w")), ("key", 0, ord("w"))]


def test_frame_file_is_replaced_whole(tmp_path):
    writer = frames.FrameFile(tmp_path / "f.rgb")
    assert writer.write(b"a" * 12) == 1
    inode = (tmp_path / "f.rgb").stat().st_ino
    assert writer.write(b"b" * 12) == 2
    assert (tmp_path / "f.rgb").read_bytes() == b"b" * 12
    assert (tmp_path / "f.rgb").stat().st_ino != inode  # a reader keeps the old frame whole
    writer.close()
    assert not list(tmp_path.iterdir())
