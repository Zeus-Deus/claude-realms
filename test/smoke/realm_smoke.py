"""Manual smoke: driver install, realm start/exec/launch/shot, frames, stop."""
import json, os, subprocess, sys, time
from claude_realms.host import ClaudeSettings
from claude_realms.service import RealmService
from realms_core import install_driver

home = os.environ["REALMS_HOME"]
os.makedirs(home, exist_ok=True)
t = time.time()
driver = install_driver.install(home, progress=lambda m: print("  driver:", m, flush=True))
print("driver", driver["version"], driver["checks"], driver["smoke"], f"{time.time()-t:.1f}s", flush=True)
svc = RealmService(home, "claude-smoke", ClaudeSettings.load(home))
print("setup", json.dumps(svc.setup_status()), flush=True)
t = time.time()
rec = svc.ensure()
print("started", rec["id"], rec["backend"], rec["renderer"], f"{time.time()-t:.1f}s", flush=True)
print("exec", svc.exec("echo hello from $XDG_CURRENT_DESKTOP; echo $WAYLAND_DISPLAY $DISPLAY; id -un"), flush=True)
print("launch", svc.launch("gtk3-demo"), flush=True)
time.sleep(3)
shot = svc.shot()
print("shot", shot["width"], shot["height"], len(shot["png"]), flush=True)
open("/tmp/shot.png", "wb").write(shot["png"])
for mode in ("raster", "image"):
    out = subprocess.run([sys.executable, "-m", "claude_realms.frames", "--socket", rec["vnc_socket"],
                          "--mode", mode, "--columns", "80", "--rows", "24", "--frames", "2"],
                         capture_output=True, text=True, timeout=30)
    lines = out.stdout.splitlines()
    print("frames", mode, out.returncode, [l[:60] for l in lines][:4], out.stderr[-300:], flush=True)
st = svc.status()
print("status live", st["live"]["id"], st["live"]["backend"], flush=True)
svc.stop()
print("stopped; records:", [(r["id"], r["status"]) for r in svc.records()], flush=True)
left = subprocess.run(["ps", "-eo", "pid,ppid,comm"], capture_output=True, text=True).stdout
print("leftover realm processes:", [l for l in left.splitlines() if any(k in l for k in ("labwc","wayvnc","Xwayland","dbus-daemon","gtk3"))], flush=True)
