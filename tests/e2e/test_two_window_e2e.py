"""End-to-end test (docs/DESIGN.md, Testing strategy): two headless Chromium windows with the
real extension on the local fake YouTube page; sync, ad, reload and error+refresh must each
end within one frame (33 ms), measured from the frame counter. Takes about 3 minutes.

Skip it for a quick run:  pytest -m "not e2e"
"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.e2e
def test_two_window_sync_on_fake_youtube():
    proc = subprocess.run([sys.executable, str(ROOT / "demo" / "e2e" / "two_window_e2e.py")],
                          cwd=ROOT, capture_output=True, text=True, timeout=600)
    tail = "\n".join(proc.stdout.splitlines()[-8:])
    assert proc.returncode == 0, f"end-to-end test failed:\n{tail}\n{proc.stderr[-2000:]}"
