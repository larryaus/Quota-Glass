import json
import select
import subprocess
import time
from typing import Any, Dict, Optional

CLIENT_NAME = "quota-glass"
CLIENT_VERSION = "1.0.0"
INITIALIZE_ID = 1
RATE_LIMITS_ID = 2
RATE_LIMITS_METHOD = "account/rateLimits/read"


class CodexCliError(Exception):
    """Base class for every Codex CLI read failure."""


class CodexCliUnavailable(CodexCliError):
    """The CLI is missing, not runnable, or not logged in."""


class CodexCliTimeout(CodexCliError):
    """The CLI did not answer within the allotted time."""


class CodexCliProtocolError(CodexCliError):
    """The CLI answered with something this reader cannot parse."""


def _send(process: subprocess.Popen, message: Dict[str, Any]) -> None:
    if process.stdin is None:
        raise CodexCliProtocolError("codex app-server has no stdin")
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()


def _read_result(
    process: subprocess.Popen,
    request_id: int,
    deadline: float,
) -> Dict[str, Any]:
    if process.stdout is None:
        raise CodexCliProtocolError("codex app-server has no stdout")
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CodexCliTimeout(
                "codex app-server did not answer request %d in time" % request_id
            )
        ready, _, _ = select.select([process.stdout], [], [], remaining)
        if not ready:
            raise CodexCliTimeout(
                "codex app-server did not answer request %d in time" % request_id
            )
        line = process.stdout.readline()
        if not line:
            raise CodexCliProtocolError("codex app-server closed its output stream")
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            # The app-server interleaves notifications; ignore anything that is
            # not a JSON-RPC frame rather than failing the whole read.
            continue
        if not isinstance(message, dict) or message.get("id") != request_id:
            continue
        if "error" in message:
            raise CodexCliUnavailable(
                "codex app-server rejected %s: %s"
                % (request_id, json.dumps(message["error"]))
            )
        result = message.get("result")
        if not isinstance(result, dict):
            raise CodexCliProtocolError(
                "codex app-server returned a non-object result for %d" % request_id
            )
        return result


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
    for stream in (process.stdin, process.stdout):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def read_account_rate_limits(
    cli_path: str = "codex",
    timeout_seconds: int = 20,
) -> Dict[str, Any]:
    """Ask the official Codex CLI for the account's current rate limits.

    Delegating to the CLI keeps auth, token refresh, and request attestation
    inside Codex. This process never reads Codex credentials.
    """
    deadline = time.monotonic() + max(1, timeout_seconds)
    try:
        process = subprocess.Popen(
            [cli_path, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        raise CodexCliUnavailable("cannot run %s app-server: %s" % (cli_path, exc))
    try:
        _send(
            process,
            {
                "jsonrpc": "2.0",
                "id": INITIALIZE_ID,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": CLIENT_NAME,
                        "title": "Quota Glass",
                        "version": CLIENT_VERSION,
                    }
                },
            },
        )
        _read_result(process, INITIALIZE_ID, deadline)
        _send(process, {"jsonrpc": "2.0", "method": "initialized", "params": {}})
        _send(
            process,
            {
                "jsonrpc": "2.0",
                "id": RATE_LIMITS_ID,
                "method": RATE_LIMITS_METHOD,
                "params": {},
            },
        )
        return _read_result(process, RATE_LIMITS_ID, deadline)
    except BrokenPipeError as exc:
        raise CodexCliUnavailable("codex app-server exited early: %s" % exc)
    finally:
        _terminate(process)
