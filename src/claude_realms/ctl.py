"""Client for the realms control socket: ``ctl.py SOCKET JSON`` prints one JSON line.

Standard library only and safe under ``python -I -S``: the mod runs it for
every /realm command and pane refresh.
"""
import json
import socket
import sys


def main(argv):
    path, request = argv[1], argv[2]
    json.loads(request)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(600)
        try:
            connection.connect(path)
        except OSError as exc:
            print(json.dumps({"texts": ["the realms server is not reachable: " + str(exc)], "isError": True,
                              "unreachable": True}))
            return 0
        connection.sendall(request.encode() + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = connection.recv(65536)
            if not chunk:
                break
            data += chunk
    sys.stdout.write(data.decode("utf-8", "replace") or json.dumps({"texts": ["no reply"], "isError": True}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
