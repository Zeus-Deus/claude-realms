"""Screenshots of the portal band while a fresh realm installs and opens (kitty, nested)."""
import sys
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from kitty_nested import ART, HOME, json, install_driver, outer_setup, shot, type_text  # noqa: E402


def main():
    install_driver.install(HOME)
    outer, record = outer_setup()
    time.sleep(25)
    type_text(record, "/realm on\n")
    for index, delay in enumerate((0.15, 0.3, 0.5, 0.8, 1.5, 3)):
        time.sleep(delay)
        shot(outer, f"portal-{index}.png")
    outer.stop()


if __name__ == "__main__":
    main()
