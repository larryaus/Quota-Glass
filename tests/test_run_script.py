import socket
import subprocess
from pathlib import Path
from typing import Iterator, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_SCRIPT = REPO_ROOT / "run.sh"
BACKEND_PORT = 8000


def _port_has_listener(port: int) -> bool:
    probe = socket.socket()
    try:
        probe.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


@pytest.fixture
def backend_port_busy() -> Iterator[None]:
    """Guarantee the backend port is taken, whether or not an app is running.

    SO_REUSEADDR is required: a sibling test leaves TIME_WAIT sockets on this
    port, and a plain bind would fail through them. If this fixture silently
    failed to hold the port, run.sh would pass its preflight and launch real
    servers, hanging the test until its timeout.
    """
    sock: Optional[socket.socket] = socket.socket()
    assert sock is not None
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", BACKEND_PORT))
        sock.listen(1)
    except OSError:
        sock.close()
        sock = None
        if not _port_has_listener(BACKEND_PORT):
            pytest.fail(
                "could not hold port %d and nothing is listening on it; "
                "refusing to run a test that would launch real servers"
                % BACKEND_PORT
            )
    try:
        yield
    finally:
        if sock is not None:
            sock.close()


def test_lingering_time_wait_socket_is_not_reported_busy() -> None:
    """A just-stopped instance leaves TIME_WAIT sockets on the backend port.

    uvicorn sets SO_REUSEADDR and binds through them, so the preflight probe
    must too. Without it the probe is stricter than the server it guards and
    refuses to start on every restart.
    """
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("127.0.0.1", BACKEND_PORT))
    except OSError:
        pytest.skip("backend port is genuinely in use by a running instance")
    listener.listen(1)

    # Closing the accepted connection from the server side puts the local
    # endpoint -- 127.0.0.1:BACKEND_PORT -- into TIME_WAIT.
    client = socket.create_connection(("127.0.0.1", BACKEND_PORT))
    conn, _ = listener.accept()
    conn.close()
    client.close()
    listener.close()

    probe = socket.socket()
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", BACKEND_PORT))
    except OSError as exc:  # pragma: no cover - platform dependent
        pytest.fail(
            "SO_REUSEADDR bind must succeed through TIME_WAIT, got: %s" % exc
        )
    finally:
        probe.close()

    # Extract just the probe function; sourcing run.sh would launch the servers.
    func_src = subprocess.run(
        ["awk", "/^port_is_busy\\(\\) \\{/,/^\\}/", str(RUN_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    assert "setsockopt" in func_src, (
        "probe must set SO_REUSEADDR to match how uvicorn binds; got:\n" + func_src
    )

    result = subprocess.run(
        [
            "bash",
            "-c",
            'REPO_DIR="%s"\n%s\nport_is_busy %d' % (REPO_ROOT, func_src, BACKEND_PORT),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    # port_is_busy exits 0 when busy; a lingering TIME_WAIT must read as free.
    assert result.returncode != 0, (
        "preflight reported the port busy while only TIME_WAIT sockets remain; "
        "uvicorn would have bound successfully:\n" + result.stdout + result.stderr
    )


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
