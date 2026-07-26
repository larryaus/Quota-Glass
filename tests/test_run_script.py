import socket
import subprocess
from pathlib import Path
from typing import Iterator, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_SCRIPT = REPO_ROOT / "run.sh"
BACKEND_PORT = 8000


@pytest.fixture
def backend_port_busy() -> Iterator[None]:
    """Guarantee the backend port is taken, whether or not an app is running."""
    sock: Optional[socket.socket] = socket.socket()
    assert sock is not None
    try:
        sock.bind(("127.0.0.1", BACKEND_PORT))
        sock.listen(1)
    except OSError:
        # A real Quota Glass instance already holds the port, which is exactly
        # the situation under test.
        sock.close()
        sock = None
    try:
        yield
    finally:
        if sock is not None:
            sock.close()


@pytest.mark.usefixtures("backend_port_busy")
def test_run_sh_refuses_to_start_when_backend_port_is_busy() -> None:
    result = subprocess.run(
        ["bash", str(RUN_SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = result.stdout + result.stderr

    assert result.returncode != 0, (
        "run.sh reported success even though the backend port was unavailable:\n"
        f"{output}"
    )
    assert str(BACKEND_PORT) in output
    assert "in use" in output.lower()


@pytest.mark.usefixtures("backend_port_busy")
def test_run_sh_names_the_process_holding_the_backend_port() -> None:
    result = subprocess.run(
        ["bash", str(RUN_SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = result.stdout + result.stderr

    assert "quota glass" in output.lower() or "already running" in output.lower(), (
        f"run.sh gave no actionable hint about the port conflict:\n{output}"
    )
