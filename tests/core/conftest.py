import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


def pytest_configure(config):
    # Markers the original suite used through Hermes' conftest.
    config.addinivalue_line("markers", "platforms(*names): platforms a test runs on")
    config.addinivalue_line("markers", "integration: starts real private desktops")
