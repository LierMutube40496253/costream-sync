"""python -m server: run the sync server with settings from .env (see .env.example).

Example: COSTREAM_HOST=0.0.0.0 makes it reachable from another PC on your network,
which then opens http://<this PC's IP>:8765/panel/
"""
import os
from pathlib import Path

import uvicorn


def load_env(path: Path) -> None:
    """Minimal .env reader (KEY=value lines); real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


if __name__ == "__main__":
    load_env(Path(__file__).resolve().parent.parent / ".env")
    uvicorn.run("server.app:app", host=os.environ.get("COSTREAM_HOST", "127.0.0.1"),
                port=int(os.environ.get("COSTREAM_PORT", "8765")))
