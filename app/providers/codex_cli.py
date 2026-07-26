import codecs
import json
import os
import select
import subprocess
import time
from typing import Any, Dict

CLIENT_NAME = "quota-glass"
CLIENT_VERSION = "1.0.0"
INITIALIZE_ID = 1
RATE_LIMITS_ID = 2
RATE_LIMITS_METHOD = "account/rateLimits/read"
_READ_CHUNK_BYTES = 4096


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


class _LineReader:
    """Assembles newline-delimited frames from a pipe without ever blocking
    past a caller-supplied deadline.

    `select()` on a stream only guarantees that *some* bytes are available,
    not a full line. `TextIOWrapper.readline()` can consume a partial line
    into its own internal buffer and then block waiting for the rest,
    bypassing the deadline entirely if the peer stalls mid-line. Reading raw
    bytes off the file descriptor ourselves means every wait for more data
    goes back through `select()` with the remaining time, so a peer that
    writes a partial line and then stalls is still bounded by the deadline.
    """

    def __init__(self, stream) -> None:
        self._fd = stream.fileno()
        self._buffer = ""
        # A stateful incremental decoder carries a partial multi-byte
        # codepoint across separate os.read() chunks. Decoding each chunk
        # independently (e.g. chunk.decode("utf-8", errors="replace")) would
        # split any character whose bytes straddle a chunk boundary into two
        # invalid fragments, each silently replaced with U+FFFD instead of
        # reassembling the original character.
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def read_line(self, deadline: float, request_id: int) -> str:
        """Return the next newline-terminated frame, or "" at EOF once
        nothing remains buffered. Raises CodexCliTimeout if the deadline
        passes before a full line (or EOF) becomes available."""
        while True:
            newline_at = self._buffer.find("\n")
            if newline_at != -1:
                line = self._buffer[: newline_at + 1]
                self._buffer = self._buffer[newline_at + 1 :]
                return line
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexCliTimeout(
                    "codex app-server did not answer request %d in time" % request_id
                )
            ready, _, _ = select.select([self._fd], [], [], remaining)
            if not ready:
                raise CodexCliTimeout(
                    "codex app-server did not answer request %d in time" % request_id
                )
            chunk = os.read(self._fd, _READ_CHUNK_BYTES)
            if not chunk:
                # EOF: flush any codepoint the decoder was still holding
                # (an incomplete trailing sequence becomes U+FFFD here,
                # rather than being silently dropped), then hand back
                # whatever partial data is left so the caller can try to
                # parse it before deciding the stream is truly closed.
                self._buffer += self._decoder.decode(b"", final=True)
                line, self._buffer = self._buffer, ""
                return line
            self._buffer += self._decoder.decode(chunk)


def _read_result(
    reader: "_LineReader",
    request_id: int,
    deadline: float,
) -> Dict[str, Any]:
    while True:
        line = reader.read_line(deadline, request_id)
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
        if process.stdout is None:
            raise CodexCliProtocolError("codex app-server has no stdout")
        reader = _LineReader(process.stdout)
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
        _read_result(reader, INITIALIZE_ID, deadline)
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
        return _read_result(reader, RATE_LIMITS_ID, deadline)
    except BrokenPipeError as exc:
        raise CodexCliUnavailable("codex app-server exited early: %s" % exc)
    finally:
        _terminate(process)
