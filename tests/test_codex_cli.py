import json
import os
import stat
import time
from pathlib import Path

import pytest

from app.providers import codex_cli
from app.providers.codex_cli import (
    CodexCliProtocolError,
    CodexCliTimeout,
    CodexCliUnavailable,
    read_account_rate_limits,
)

RATE_LIMITS_RESULT = {
    "rateLimits": {
        "limitId": "codex",
        "primary": {
            "usedPercent": 18,
            "windowDurationMins": 10080,
            "resetsAt": 1785621948,
        },
        "secondary": None,
        "credits": {"hasCredits": False, "unlimited": False, "balance": 0},
        "individualLimit": None,
        "spendControlReached": False,
        "planType": "plus",
    }
}


def _write_fake_cli(tmp_path: Path, body: str) -> str:
    """Write an executable stand-in for `codex app-server`."""
    script = tmp_path / "fake-codex"
    script.write_text("#!/usr/bin/env python3\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


RESPONDER = """
import json, sys

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    message = json.loads(line)
    if "id" not in message:
        continue
    if message["method"] == "initialize":
        result = {"userAgent": "fake", "codexHome": "/tmp"}
    else:
        result = %s
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\\n")
    sys.stdout.flush()
""" % repr(RATE_LIMITS_RESULT)


def test_reads_rate_limits(tmp_path):
    cli = _write_fake_cli(tmp_path, RESPONDER)

    result = read_account_rate_limits(cli, timeout_seconds=10)

    assert result["rateLimits"]["primary"]["usedPercent"] == 18
    assert result["rateLimits"]["planType"] == "plus"


def test_missing_binary_is_unavailable(tmp_path):
    with pytest.raises(CodexCliUnavailable):
        read_account_rate_limits(str(tmp_path / "does-not-exist"), timeout_seconds=5)


def test_logged_out_error_response_is_unavailable(tmp_path):
    body = """
import json, sys

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    message = json.loads(line)
    if "id" not in message:
        continue
    if message["method"] == "initialize":
        result = {"jsonrpc": "2.0", "id": message["id"], "result": {}}
    else:
        result = {"jsonrpc": "2.0", "id": message["id"],
                  "error": {"code": -32000, "message": "not logged in"}}
    sys.stdout.write(json.dumps(result) + "\\n")
    sys.stdout.flush()
"""
    cli = _write_fake_cli(tmp_path, body)

    with pytest.raises(CodexCliUnavailable) as excinfo:
        read_account_rate_limits(cli, timeout_seconds=10)

    assert "not logged in" in str(excinfo.value)


def test_hang_times_out(tmp_path):
    body = """
import time
time.sleep(30)
"""
    cli = _write_fake_cli(tmp_path, body)

    with pytest.raises(CodexCliTimeout):
        read_account_rate_limits(cli, timeout_seconds=2)


def test_malformed_output_is_protocol_error(tmp_path):
    body = """
import sys
sys.stdout.write("this is not json\\n")
sys.stdout.flush()
"""
    cli = _write_fake_cli(tmp_path, body)

    with pytest.raises((CodexCliProtocolError, CodexCliTimeout)):
        read_account_rate_limits(cli, timeout_seconds=3)


def test_partial_line_stall_times_out_promptly(tmp_path):
    """A peer that writes a partial (unterminated) line and then stalls must
    still be bounded by the deadline, not left to block on readline()."""
    body = """
import json, sys, time

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    message = json.loads(line)
    if "id" not in message:
        continue
    if message["method"] == "initialize":
        result = {"userAgent": "fake", "codexHome": "/tmp"}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\\n")
        sys.stdout.flush()
    else:
        sys.stdout.write('{"jsonrpc": "2.0", "id": 2, "resu')
        sys.stdout.flush()
        time.sleep(30)
"""
    cli = _write_fake_cli(tmp_path, body)

    start = time.monotonic()
    with pytest.raises(CodexCliTimeout):
        read_account_rate_limits(cli, timeout_seconds=2)
    elapsed = time.monotonic() - start

    assert elapsed < 5, (
        "expected the partial-line stall to time out near the configured "
        "deadline, but the call took %.2fs" % elapsed
    )


def _write_all(fd, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def test_line_reader_reassembles_multibyte_char_split_across_chunk_boundary():
    """A UTF-8 character whose bytes straddle a _READ_CHUNK_BYTES boundary
    must come out intact, not as two U+FFFD replacement characters.

    Decoding each os.read() chunk independently has no memory of a dangling
    partial codepoint from the previous chunk, so a character split right at
    the chunk boundary gets corrupted silently (json.loads happily accepts
    U+FFFD, so this would not raise anywhere) instead of raising loudly.
    """
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "rb", buffering=0)
    try:
        # 'e with acute' is 0xC3 0xA9 in UTF-8. Place the first byte as the
        # very last byte of the first _READ_CHUNK_BYTES-sized read, and the
        # second byte as the first byte of the next read.
        prefix = b"a" * (codex_cli._READ_CHUNK_BYTES - 1)
        multibyte = "é".encode("utf-8")
        payload = prefix + multibyte + b"tail\n"
        _write_all(write_fd, payload)

        reader = codex_cli._LineReader(stream)
        deadline = time.monotonic() + 5
        line = reader.read_line(deadline, request_id=1)

        expected = "a" * (codex_cli._READ_CHUNK_BYTES - 1) + "é" + "tail\n"
        assert line == expected
        assert "�" not in line
    finally:
        os.close(write_fd)
        stream.close()
