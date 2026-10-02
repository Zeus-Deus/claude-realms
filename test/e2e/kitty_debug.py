"""Run the nested kitty view and print kitty's own stderr (why a frame is refused)."""
import sys, time
sys.path.insert(0, __file__.rsplit("/", 1)[0])
from kitty_nested import ART, HOME, install_driver, outer_setup, shot, type_text  # noqa: E402

install_driver.install(HOME)
outer, record = outer_setup()
time.sleep(25)
for command, wait in (("/realm on", 10), ("/realm launch gtk3-demo", 8)):
    type_text(record, command + "\n")
    time.sleep(wait)
shot(outer, "kitty-debug.png")
import glob, os
runtime = record["runtime_dir"]
for path in sorted(glob.glob(runtime + "/*.stderr")):
    text = open(path, errors="replace").read()
    if "kitty" in text.lower() or "graphics" in text.lower() or "image" in text.lower():
        print("==", os.path.basename(path), "\n", text[-3000:])
outer.stop()
