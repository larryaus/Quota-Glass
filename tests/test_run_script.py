"""Tests for run.sh's backend-port preflight.

Every test drives a shell function extracted from run.sh rather than running
the script. Running it risks launching real uvicorn and vite servers, and
`set -m` puts those children in their own process groups, so killing the test's
bash — which is all `subprocess.run(timeout=...)` does — leaves them orphaned.
Extraction also frees the tests from port 8000 entirely, so they verify the
same behaviour whether or not a developer has an instance running.
"""

import os
import socket
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_SCRIPT = REPO_ROOT / "run.sh"


def _shell_function(name: str) -> str:
    extracted = subprocess.run(
        ["awk", "/^%s\\(\\) \\{/,/^\\}/" % name, str(RUN_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout
    assert extracted.strip(), "run.sh no longer defines %s()" % name
    return extracted


def _run_shell(body: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "bash",
            "-c",
            'set -euo pipefail\nREPO_DIR="%s"\n%s' % (REPO_ROOT, body),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


@contextmanager
def _listening_port() -> Iterator[int]:
    """Hold an ephemeral port for the duration of the block."""
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        yield listener.getsockname()[1]
    finally:
        listener.close()


def _free_port() -> int:
    """Return a port with no listener and no lingering sockets."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _port_in_time_wait() -> int:
    """Return a port whose only remaining sockets are in TIME_WAIT.

    Closing the accepted connection from the server side leaves the local
    endpoint -- 127.0.0.1:port -- in TIME_WAIT after the listener is gone.
    """
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.listen(1)
    client = socket.create_connection(("127.0.0.1", port))
    conn, _ = listener.accept()
    conn.close()
    client.close()
    listener.close()
    return port


def test_port_is_busy_detects_a_listening_socket() -> None:
    """Guards the sibling tests: a probe that never reports busy is useless."""
    func_src = _shell_function("port_is_busy")

    with _listening_port() as port:
        result = _run_shell("%s\nport_is_busy %d" % (func_src, port))

    assert result.returncode == 0, (
        "preflight missed a real listener on port %d:\n%s"
        % (port, result.stdout + result.stderr)
    )


def test_lingering_time_wait_socket_is_not_reported_busy() -> None:
    """A just-stopped instance leaves TIME_WAIT sockets on the backend port.

    uvicorn sets SO_REUSEADDR and binds through them, so the preflight probe
    must too. Without it the probe is stricter than the server it guards and
    refuses to start on every restart.
    """
    func_src = _shell_function("port_is_busy")
    assert "setsockopt" in func_src, (
        "probe must set SO_REUSEADDR to match how uvicorn binds; got:\n" + func_src
    )

    port = _port_in_time_wait()

    plain = socket.socket()
    try:
        plain.bind(("127.0.0.1", port))
        raise AssertionError(
            "port %d is not in TIME_WAIT, so this test proves nothing" % port
        )
    except OSError:
        pass
    finally:
        plain.close()

    result = _run_shell("%s\nport_is_busy %d" % (func_src, port))

    # port_is_busy exits 0 when busy; a lingering TIME_WAIT must read as free.
    assert result.returncode != 0, (
        "preflight reported the port busy while only TIME_WAIT sockets remain; "
        "uvicorn would have bound successfully:\n" + result.stdout + result.stderr
    )


def _preflight_body(port: int) -> str:
    return "\n".join(
        [
            _shell_function("port_is_busy"),
            _shell_function("port_holder"),
            _shell_function("preflight_backend_port"),
            "preflight_backend_port %d" % port,
        ]
    )


def test_preflight_refuses_and_names_the_process_holding_the_port() -> None:
    """The refusal must identify the holder, not just say "in use"."""
    with _listening_port() as port:
        result = _run_shell(_preflight_body(port))

    output = result.stdout + result.stderr
    assert result.returncode != 0, (
        "preflight reported success even though the port was unavailable:\n"
        + output
    )
    assert str(port) in output
    assert "in use" in output.lower()
    assert "already running" in output.lower()
    # This test process is the holder, so only a working lookup can name it.
    assert "(PID %d)" % os.getpid() in output, (
        "preflight named no holder for the busy port:\n" + output
    )


def test_preflight_allows_a_free_port() -> None:
    result = _run_shell(_preflight_body(_free_port()))

    assert result.returncode == 0, (
        "preflight refused a free port:\n" + result.stdout + result.stderr
    )
    assert result.stdout.strip() == ""


def test_run_sh_gates_startup_on_the_preflight() -> None:
    """The extracted-function tests only matter if run.sh still calls it."""
    source = RUN_SCRIPT.read_text(encoding="utf-8")

    assert 'preflight_backend_port "$BACKEND_PORT" || exit 1' in source, (
        "run.sh must refuse to start when the backend port preflight fails"
    )
