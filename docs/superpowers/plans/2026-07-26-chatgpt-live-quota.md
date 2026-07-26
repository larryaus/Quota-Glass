# ChatGPT Live Quota Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the ChatGPT provider a live quota source by delegating to the official Codex CLI, falling back to today's rollout-file parsing on any failure.

**Architecture:** A new `app/providers/codex_cli.py` spawns `codex app-server` and calls its `account/rateLimits/read` JSON-RPC method. `parse_chatgpt` gains a live path guarded by a shared `LiveSourceCache` (extracted from the existing `ClaudeOAuthCache`) so calls are rate-limited and backed off. Quota Glass never reads Codex tokens — the CLI owns auth, refresh, and Cloudflare attestation.

**Tech Stack:** Python 3.9.13, pydantic 2.11.7, FastAPI 0.116.1, pytest 8.4.1, React 18.3.1, TypeScript 5.9.3, Vite 5.4.21.

## Global Constraints

- **Python 3.9.13.** No `X | Y` union syntax, no `list[str]` builtin generics. Use `typing.Optional`, `typing.List`, `typing.Dict`, `typing.Tuple`.
- **House style is `%`-formatting** for messages (`"failed: %s" % exc`), matching `app/poller.py` and `app/providers/chatgpt.py`.
- **Tests never touch the network, Keychain, the real Codex CLI, real usage directories, or macOS notifications.** README.md:137-139 promises this. Fake CLI executables are written to `tmp_path`.
- **Meter keys must be byte-identical between live and rollout modes** (`chatgpt.primary`, `chatgpt.secondary`, `chatgpt.individual_limit`). `Database.add_samples` keys history by `meter.key` and `AlertEngine` tracks threshold state by the same key; divergent keys would split history and misfire alerts.
- **`ENABLE_CHATGPT_LIVE` defaults to `0`.** No subprocess spawn or network call in default behaviour.
- **Never log or persist Codex response bodies or any part of `~/.codex/auth.json`.**
- Run the full suite with `.venv/bin/python -m pytest -q` from the repo root.
- **Test-count baseline is 60 passing tests** (at commit `b30a25e`). Each task states the expected running total: 61, 67, 72, 75, 79, 81. A different starting count means the baseline moved — recompute rather than assuming a regression.

---

### Task 1: Make rollout snapshot selection count usable files

Today `_latest_snapshot` keeps the N most recently modified rollout files, then looks for a usable snapshot inside them. Sessions that never emitted `rate_limits` still consume slots, so good data one slot below the cutoff is invisible. The live path falls back to this parser, so it must be reliable first.

**Files:**
- Modify: `app/providers/chatgpt.py:94-116`
- Test: `tests/test_providers.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `_latest_snapshot(root: Path, candidate_file_count: int = 5) -> Optional[JsonDict]` — unchanged signature, corrected behaviour.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_providers.py`:

```python
def test_latest_snapshot_skips_files_without_rate_limits(tmp_path):
    from app.providers.chatgpt import _latest_snapshot

    sessions = tmp_path / "sessions" / "2026" / "07" / "26"
    sessions.mkdir(parents=True)

    def write(name, mtime, with_limits):
        path = sessions / ("rollout-%s.jsonl" % name)
        records = [
            {
                "type": "event_msg",
                "timestamp": "2026-07-26T04:00:00Z",
                "payload": {"type": "agent_message", "message": "x"},
            }
        ]
        if with_limits:
            records.append(
                {
                    "type": "event_msg",
                    "timestamp": "2026-07-26T00:00:00Z",
                    "payload": {
                        "type": "token_count",
                        "rate_limits": {"primary": {"used_percent": 42}},
                        "info": {},
                    },
                }
            )
        path.write_text("\n".join(json.dumps(r) for r in records))
        os.utime(path, (mtime, mtime))

    for index in range(5):
        write("recent-%d" % index, 2000 + index, with_limits=False)
    write("older-with-limits", 1000, with_limits=True)

    snapshot = _latest_snapshot(tmp_path / "sessions", 5)

    assert snapshot is not None
    assert snapshot["payload"]["rate_limits"]["primary"]["used_percent"] == 42
```

Ensure `import json` and `import os` are present at the top of the file.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_providers.py::test_latest_snapshot_skips_files_without_rate_limits -v`
Expected: FAIL — `assert None is not None`, because the five snapshot-less files consume every candidate slot.

- [ ] **Step 3: Write minimal implementation**

Add the module constant next to `DEFAULT_CANDIDATE_FILE_COUNT` (`app/providers/chatgpt.py:19`):

```python
MAX_SCAN_MULTIPLIER = 10
```

Replace `_latest_snapshot` (lines 94-116) with:

```python
def _latest_snapshot(
    root: Path,
    candidate_file_count: int = DEFAULT_CANDIDATE_FILE_COUNT,
) -> Optional[JsonDict]:
    if not root.exists():
        return None
    wanted = max(1, candidate_file_count)
    paths = sorted(
        root.glob("*/*/*/rollout-*.jsonl"),
        key=_file_mtime,
        reverse=True,
    )[: wanted * MAX_SCAN_MULTIPLIER]
    candidates: List[Tuple[JsonDict, Optional[datetime]]] = []
    for path in paths:
        record = _latest_usable_snapshot(path)
        if record is None:
            continue
        candidates.append((record, _parse_timestamp(record.get("timestamp"))))
        if len(candidates) >= wanted:
            break
    timestamped = [candidate for candidate in candidates if candidate[1] is not None]
    if timestamped:
        return max(
            timestamped,
            key=lambda candidate: candidate[1],
        )[0]
    return candidates[0][0] if candidates else None
```

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — 61 passed. The existing snapshot tests must stay green; `wanted` snapshots are still collected newest-mtime-first.

- [ ] **Step 5: Commit**

```bash
git add app/providers/chatgpt.py tests/test_providers.py
git commit -m "Count usable snapshots, not files, when picking rollout data"
```

---

### Task 2: Extract the shared live-source cache

`ClaudeOAuthCache` is provider-agnostic apart from classifying HTTP 429 / `Retry-After`. Extract the reusable core so the Codex reader gets min-interval, backoff, and cached-payload retention without duplication.

**Files:**
- Create: `app/providers/live_cache.py`
- Modify: `app/providers/claude.py:306-385`
- Test: `tests/test_live_cache.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `LiveSourceCache(label: str, min_interval_seconds: int = 300, max_backoff_seconds: int = 3600)`
  - `.age_seconds(current: datetime) -> Optional[int]`
  - `.should_attempt(current: datetime) -> bool`
  - `.is_backed_off(current: datetime) -> bool`
  - `.record_success(payload: Dict[str, Any], current: datetime) -> None`
  - `.record_failure(exc: Exception, current: datetime) -> None`
  - `.payload: Optional[Dict[str, Any]]`, `.next_retry_at: Optional[datetime]`, `.backoff_reason: Optional[str]`
  - Subclass hook: `_classify_failure(exc: Exception, current: datetime) -> Tuple[int, str]` returning `(delay_seconds, reason)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_live_cache.py`:

```python
from datetime import datetime, timedelta, timezone

from app.providers.live_cache import LiveSourceCache


def _now():
    return datetime(2026, 7, 26, 5, 0, 0, tzinfo=timezone.utc)


def test_first_attempt_is_allowed():
    cache = LiveSourceCache("Codex CLI", min_interval_seconds=300)
    assert cache.should_attempt(_now()) is True


def test_success_suppresses_attempts_until_min_interval():
    cache = LiveSourceCache("Codex CLI", min_interval_seconds=300)
    current = _now()
    cache.record_success({"ok": True}, current)

    assert cache.should_attempt(current + timedelta(seconds=299)) is False
    assert cache.should_attempt(current + timedelta(seconds=300)) is True
    assert cache.age_seconds(current + timedelta(seconds=60)) == 60
    assert cache.payload == {"ok": True}


def test_failure_backs_off_and_reports_reason():
    cache = LiveSourceCache("Codex CLI", min_interval_seconds=300)
    current = _now()
    cache.record_failure(RuntimeError("boom"), current)

    assert cache.is_backed_off(current + timedelta(seconds=10)) is True
    assert "Codex CLI" in cache.backoff_reason
    assert "boom" in cache.backoff_reason
    assert cache.should_attempt(current + timedelta(seconds=10)) is False
    assert cache.should_attempt(current + timedelta(seconds=300)) is True


def test_repeated_failures_grow_the_delay():
    cache = LiveSourceCache(
        "Codex CLI",
        min_interval_seconds=100,
        max_backoff_seconds=400,
    )
    current = _now()
    for _ in range(4):
        cache.record_failure(RuntimeError("boom"), current)
    delay = (cache.next_retry_at - current).total_seconds()

    assert delay == 400


def test_success_clears_backoff():
    cache = LiveSourceCache("Codex CLI", min_interval_seconds=300)
    current = _now()
    cache.record_failure(RuntimeError("boom"), current)
    cache.record_success({"ok": True}, current)

    assert cache.next_retry_at is None
    assert cache.backoff_reason is None
    assert cache.is_backed_off(current) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_live_cache.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.providers.live_cache'`

- [ ] **Step 3: Write minimal implementation**

Create `app/providers/live_cache.py`:

```python
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple


class LiveSourceCache:
    """Rate-limits calls to a live usage source and retains the last success.

    A previously successful payload stays available while the source is backed
    off, so a transient failure degrades to a stale reading rather than to no
    reading at all.
    """

    def __init__(
        self,
        label: str,
        min_interval_seconds: int = 300,
        max_backoff_seconds: int = 3600,
    ) -> None:
        self.label = label
        self.min_interval_seconds = max(1, min_interval_seconds)
        self.max_backoff_seconds = max(
            self.min_interval_seconds,
            max_backoff_seconds,
        )
        self.payload: Optional[Dict[str, Any]] = None
        self.fetched_at: Optional[datetime] = None
        self.next_retry_at: Optional[datetime] = None
        self.backoff_reason: Optional[str] = None
        self._consecutive_failures = 0

    def age_seconds(self, current: datetime) -> Optional[int]:
        if self.fetched_at is None:
            return None
        return max(0, int((current - self.fetched_at).total_seconds()))

    def should_attempt(self, current: datetime) -> bool:
        if self.next_retry_at is not None and current < self.next_retry_at:
            return False
        age = self.age_seconds(current)
        return age is None or age >= self.min_interval_seconds

    def is_backed_off(self, current: datetime) -> bool:
        return (
            self.next_retry_at is not None
            and current < self.next_retry_at
            and self.backoff_reason is not None
        )

    def record_success(
        self,
        payload: Dict[str, Any],
        current: datetime,
    ) -> None:
        self.payload = payload
        self.fetched_at = current
        self.next_retry_at = None
        self.backoff_reason = None
        self._consecutive_failures = 0

    def _exponential_backoff_seconds(self) -> int:
        delay = self.min_interval_seconds
        for _ in range(max(0, self._consecutive_failures - 1)):
            if delay >= self.max_backoff_seconds:
                return self.max_backoff_seconds
            delay = min(self.max_backoff_seconds, delay * 2)
        return delay

    def _classify_failure(
        self,
        exc: Exception,
        current: datetime,
    ) -> Tuple[int, str]:
        """Return (delay_seconds, reason).

        Owns its own failure counting: a subclass may decide some failures
        should not escalate the backoff at all.
        """
        self._consecutive_failures += 1
        delay = self._exponential_backoff_seconds()
        return delay, "%s request failed: %s" % (self.label, exc)

    def record_failure(
        self,
        exc: Exception,
        current: datetime,
    ) -> None:
        delay, reason = self._classify_failure(exc, current)
        self.backoff_reason = reason
        self.next_retry_at = current + timedelta(seconds=delay)
```

**Why `_classify_failure` counts rather than `record_failure`:** the Claude
subclass escalates backoff only for HTTP 429 and leaves the counter untouched
for other failures. If the base class incremented unconditionally, the sequence
`429, 429, non-429, 429` would produce a different delay than today's code
does. No existing test covers that mix, so the regression would be silent.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_live_cache.py -v`
Expected: PASS — 5 passed.

- [ ] **Step 5: Reroute `ClaudeOAuthCache` onto the shared base**

In `app/providers/claude.py`, replace the whole `ClaudeOAuthCache` class body (lines 306-385) with a subclass that keeps its 429 handling. Add `from app.providers.live_cache import LiveSourceCache` to the imports.

```python
class ClaudeOAuthCache(LiveSourceCache):
    def __init__(
        self,
        min_interval_seconds: int = 300,
        max_backoff_seconds: int = 3600,
    ) -> None:
        super().__init__(
            "Claude OAuth",
            min_interval_seconds=min_interval_seconds,
            max_backoff_seconds=max_backoff_seconds,
        )

    def _classify_failure(
        self,
        exc: Exception,
        current: datetime,
    ) -> Tuple[int, str]:
        is_rate_limit = (
            isinstance(exc, httpx.HTTPStatusError)
            and exc.response.status_code == 429
        )
        if not is_rate_limit:
            # Non-429 failures retry at the floor interval and must NOT touch
            # the counter, matching the behaviour this class had before the
            # base class existed.
            return (
                self.min_interval_seconds,
                "Claude OAuth request failed: %s" % exc,
            )
        self._consecutive_failures += 1
        retry_after = _retry_after_seconds(exc, current)
        if retry_after is None:
            delay = self._exponential_backoff_seconds()
            retry_detail = "exponential backoff %ds" % delay
        else:
            delay = retry_after
            retry_detail = "Retry-After %ds" % delay
        return (
            delay,
            "Rate limited by Claude OAuth (429 Too Many Requests; %s): %s"
            % (retry_detail, exc),
        )
```

Confirm `Tuple` is in the `typing` import list in `claude.py`.

Then add this regression test to `tests/test_live_cache.py`. It pins the
counting rule that the refactor could silently break — no existing test mixes
non-429 failures into a 429 sequence:

```python
def test_claude_non_rate_limit_failure_does_not_escalate_backoff():
    import httpx

    from app.providers.claude import ClaudeOAuthCache

    cache = ClaudeOAuthCache(min_interval_seconds=300, max_backoff_seconds=3600)
    current = _now()

    def rate_limited():
        response = httpx.Response(
            429,
            request=httpx.Request("GET", "https://example.test"),
        )
        return httpx.HTTPStatusError(
            "429",
            request=response.request,
            response=response,
        )

    cache.record_failure(rate_limited(), current)
    first = (cache.next_retry_at - current).total_seconds()
    cache.record_failure(rate_limited(), current)
    second = (cache.next_retry_at - current).total_seconds()
    cache.record_failure(RuntimeError("network down"), current)
    plain = (cache.next_retry_at - current).total_seconds()
    cache.record_failure(rate_limited(), current)
    third = (cache.next_retry_at - current).total_seconds()

    assert first == 300
    assert second == 600
    assert plain == 300, "a non-429 failure retries at the floor interval"
    assert third == 1200, "a non-429 failure must not reset 429 escalation"
```

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — 67 passed. `tests/test_claude_oauth_cache.py` must be green **without edits**; if it fails, the refactor changed Claude behaviour and must be corrected, not the test.

- [ ] **Step 7: Commit**

```bash
git add app/providers/live_cache.py app/providers/claude.py tests/test_live_cache.py
git commit -m "Extract LiveSourceCache from ClaudeOAuthCache"
```

---

### Task 3: Read account rate limits from the Codex CLI

**Files:**
- Create: `app/providers/codex_cli.py`
- Test: `tests/test_codex_cli.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `read_account_rate_limits(cli_path: str = "codex", timeout_seconds: int = 20) -> Dict[str, Any]` returning the raw JSON-RPC `result`.
  - `CodexCliError` (base), `CodexCliUnavailable`, `CodexCliTimeout`, `CodexCliProtocolError`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_codex_cli.py`:

```python
import json
import os
import stat
from pathlib import Path

import pytest

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
""" % json.dumps(RATE_LIMITS_RESULT)


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_codex_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.providers.codex_cli'`

- [ ] **Step 3: Write minimal implementation**

Create `app/providers/codex_cli.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_codex_cli.py -v`
Expected: PASS — 5 passed. The timeout test should finish in roughly 2 seconds, proving the deadline is enforced and the child is reaped.

- [ ] **Step 5: Commit**

```bash
git add app/providers/codex_cli.py tests/test_codex_cli.py
git commit -m "Add Codex CLI reader for account rate limits"
```

---

### Task 4: Add the configuration knobs

**Files:**
- Modify: `app/settings.py`
- Test: `tests/test_settings.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces on `Settings`: `enable_chatgpt_live: bool`, `chatgpt_live_min_interval_seconds: int`, `codex_cli_path: str`, `codex_cli_timeout_seconds: int`. Constructor keyword arguments of the same names, each `Optional` and defaulting to the env-var lookup, matching the existing pattern.

- [ ] **Step 1: Write the failing test**

Create `tests/test_settings.py`:

```python
from app.settings import Settings


def test_chatgpt_live_defaults_off(monkeypatch):
    monkeypatch.delenv("ENABLE_CHATGPT_LIVE", raising=False)
    settings = Settings()

    assert settings.enable_chatgpt_live is False
    assert settings.chatgpt_live_min_interval_seconds == 300
    assert settings.codex_cli_path == "codex"
    assert settings.codex_cli_timeout_seconds == 20


def test_chatgpt_live_reads_environment(monkeypatch):
    monkeypatch.setenv("ENABLE_CHATGPT_LIVE", "1")
    monkeypatch.setenv("CHATGPT_LIVE_MIN_INTERVAL_SECONDS", "45")
    monkeypatch.setenv("CODEX_CLI_PATH", "/usr/local/bin/codex")
    monkeypatch.setenv("CODEX_CLI_TIMEOUT_SECONDS", "7")
    settings = Settings()

    assert settings.enable_chatgpt_live is True
    assert settings.chatgpt_live_min_interval_seconds == 45
    assert settings.codex_cli_path == "/usr/local/bin/codex"
    assert settings.codex_cli_timeout_seconds == 7


def test_explicit_arguments_beat_environment(monkeypatch):
    monkeypatch.setenv("ENABLE_CHATGPT_LIVE", "1")
    settings = Settings(enable_chatgpt_live=False)

    assert settings.enable_chatgpt_live is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_settings.py -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'enable_chatgpt_live'`

- [ ] **Step 3: Write minimal implementation**

Add four parameters to `Settings.__init__` after `oauth_max_backoff_seconds` (`app/settings.py:49`):

```python
        enable_chatgpt_live: Optional[bool] = None,
        chatgpt_live_min_interval_seconds: Optional[int] = None,
        codex_cli_path: Optional[str] = None,
        codex_cli_timeout_seconds: Optional[int] = None,
```

Append to the body, after the `oauth_max_backoff_seconds` assignment:

```python
        self.enable_chatgpt_live = (
            _bool_env("ENABLE_CHATGPT_LIVE", False)
            if enable_chatgpt_live is None
            else enable_chatgpt_live
        )
        self.chatgpt_live_min_interval_seconds = max(
            1,
            _int_env("CHATGPT_LIVE_MIN_INTERVAL_SECONDS", 300)
            if chatgpt_live_min_interval_seconds is None
            else chatgpt_live_min_interval_seconds,
        )
        self.codex_cli_path = (
            os.getenv("CODEX_CLI_PATH", "codex")
            if codex_cli_path is None
            else codex_cli_path
        )
        self.codex_cli_timeout_seconds = max(
            1,
            _int_env("CODEX_CLI_TIMEOUT_SECONDS", 20)
            if codex_cli_timeout_seconds is None
            else codex_cli_timeout_seconds,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_settings.py -v`
Expected: PASS — 3 passed.

- [ ] **Step 5: Commit**

```bash
git add app/settings.py tests/test_settings.py
git commit -m "Add ChatGPT live-source settings"
```

---

### Task 5: Give `parse_chatgpt` a live path with rollout fallback

**Files:**
- Modify: `app/providers/chatgpt.py:303-393`
- Create: `tests/fixtures/chatgpt/account-rate-limits.json`
- Test: `tests/test_providers.py`

**Interfaces:**
- Consumes: `read_account_rate_limits`, `CodexCliError` (Task 3); `LiveSourceCache` (Task 2).
- Produces: `parse_chatgpt(sessions_dir, stale_after_minutes=30, now=None, candidate_file_count=5, enable_live=False, live_cache=None, cli_path="codex", cli_timeout_seconds=20) -> ProviderState`.

- [ ] **Step 1: Add the fixture**

Create `tests/fixtures/chatgpt/account-rate-limits.json` — a redacted capture of a real `account/rateLimits/read` result:

```json
{
  "rateLimits": {
    "limitId": "codex",
    "limitName": null,
    "primary": {
      "usedPercent": 18,
      "windowDurationMins": 10080,
      "resetsAt": 1785621948
    },
    "secondary": null,
    "credits": {
      "hasCredits": false,
      "unlimited": false,
      "balance": 0
    },
    "individualLimit": null,
    "spendControlReached": false,
    "planType": "plus",
    "rateLimitReachedType": null
  },
  "rateLimitsByLimitId": {},
  "rateLimitResetCredits": {
    "availableCount": 0,
    "credits": []
  }
}
```

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_providers.py`:

```python
def test_chatgpt_live_meters_use_rollout_meter_keys(tmp_path, fixture_dir):
    from app.providers.chatgpt import parse_chatgpt
    from app.providers.live_cache import LiveSourceCache

    with (fixture_dir / "chatgpt" / "account-rate-limits.json").open() as handle:
        payload = json.load(handle)

    def fake_reader(cli_path, timeout_seconds):
        return payload

    state = parse_chatgpt(
        tmp_path,
        enable_live=True,
        live_cache=LiveSourceCache("Codex CLI"),
        live_reader=fake_reader,
    )

    assert state.mode == "oauth"
    assert state.plan_type == "plus"
    keys = [meter.key for meter in state.meters]
    assert keys == ["chatgpt.primary"]
    meter = state.meters[0]
    assert meter.used_pct == 18
    assert meter.window_minutes == 10080
    assert meter.resets_at == 1785621948
    assert meter.source == "app-server"
    assert meter.stale is False


def test_chatgpt_falls_back_to_rollout_when_live_fails(tmp_path, fixture_dir):
    from app.providers.chatgpt import parse_chatgpt
    from app.providers.codex_cli import CodexCliUnavailable
    from app.providers.live_cache import LiveSourceCache

    sessions = tmp_path / "2026" / "07" / "25"
    sessions.mkdir(parents=True)
    source = fixture_dir / "chatgpt" / "2026" / "07" / "25" / "rollout-example.jsonl"
    (sessions / "rollout-example.jsonl").write_text(source.read_text())

    def failing_reader(cli_path, timeout_seconds):
        raise CodexCliUnavailable("codex is not logged in")

    state = parse_chatgpt(
        tmp_path,
        enable_live=True,
        live_cache=LiveSourceCache("Codex CLI"),
        live_reader=failing_reader,
    )

    assert state.mode == "local"
    assert state.meters, "rollout meters must still render when live fails"
    assert state.meters[0].source == "rollout"


def test_chatgpt_serves_cached_live_reading_during_backoff(tmp_path, fixture_dir):
    from app.providers.chatgpt import parse_chatgpt
    from app.providers.codex_cli import CodexCliTimeout
    from app.providers.live_cache import LiveSourceCache

    with (fixture_dir / "chatgpt" / "account-rate-limits.json").open() as handle:
        payload = json.load(handle)
    cache = LiveSourceCache("Codex CLI", min_interval_seconds=0)

    parse_chatgpt(
        tmp_path,
        enable_live=True,
        live_cache=cache,
        live_reader=lambda cli_path, timeout_seconds: payload,
    )

    def failing_reader(cli_path, timeout_seconds):
        raise CodexCliTimeout("codex app-server hung")

    state = parse_chatgpt(
        tmp_path,
        enable_live=True,
        live_cache=cache,
        live_reader=failing_reader,
    )

    assert state.mode == "oauth"
    assert state.oauth_backed_off is True
    assert "hung" in state.oauth_backoff_reason
    assert state.meters[0].used_pct == 18


def test_chatgpt_live_disabled_keeps_local_mode(tmp_path, fixture_dir):
    from app.providers.chatgpt import parse_chatgpt

    sessions = tmp_path / "2026" / "07" / "25"
    sessions.mkdir(parents=True)
    source = fixture_dir / "chatgpt" / "2026" / "07" / "25" / "rollout-example.jsonl"
    (sessions / "rollout-example.jsonl").write_text(source.read_text())

    def exploding_reader(cli_path, timeout_seconds):
        raise AssertionError("live reader must not run when disabled")

    state = parse_chatgpt(tmp_path, live_reader=exploding_reader)

    assert state.mode == "local"
    assert state.meters[0].source == "rollout"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_providers.py -k chatgpt_live -v`
Expected: FAIL — `TypeError: parse_chatgpt() got an unexpected keyword argument 'enable_live'`

- [ ] **Step 4: Hoist shared helpers to module scope**

In `app/providers/chatgpt.py`, move the labels dict out of `parse_chatgpt` (currently lines 332-336) to module scope beside `USAGE_COMPONENTS`, and add two coercion helpers shared by both paths:

```python
WINDOW_LABELS = {
    "primary": "Primary limit",
    "secondary": "Secondary limit",
    "individual_limit": "Individual limit",
}
LIVE_WINDOW_KEYS: Tuple[Tuple[str, str], ...] = (
    ("primary", "primary"),
    ("secondary", "secondary"),
    ("individualLimit", "individual_limit"),
)


def _clamp_percent(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
```

Replace the inline coercions in the existing rollout loop (lines 341-355) with `_clamp_percent(window.get("used_percent"))`, `_optional_int(window.get("window_minutes"))`, and `_optional_int(window.get("resets_at"))`, and change `labels[window_key]` to `WINDOW_LABELS[window_key]`.

- [ ] **Step 5: Add the live mappers**

```python
def _live_meters(rate_limits: JsonDict) -> List[Meter]:
    meters: List[Meter] = []
    for source_key, meter_suffix in LIVE_WINDOW_KEYS:
        window = rate_limits.get(source_key)
        if not isinstance(window, dict):
            continue
        meters.append(
            Meter(
                key="chatgpt.%s" % meter_suffix,
                provider="chatgpt",
                label=WINDOW_LABELS[meter_suffix],
                used_pct=_clamp_percent(window.get("usedPercent")),
                window_minutes=_optional_int(window.get("windowDurationMins")),
                resets_at=_optional_int(window.get("resetsAt")),
                has_quota=True,
                source="app-server",
                stale=False,
            )
        )
    return meters


def _live_credits(rate_limits: JsonDict) -> Credits:
    raw = rate_limits.get("credits")
    if not isinstance(raw, dict):
        raw = {}
    return Credits(
        has_credits=bool(raw.get("hasCredits", False)),
        unlimited=bool(raw.get("unlimited", False)),
        balance=str(raw.get("balance", "0")),
        spend_limit_reached=bool(rate_limits.get("spendControlReached", False)),
    )
```

- [ ] **Step 6: Rename the existing body and add the orchestrator**

Rename the current `parse_chatgpt` (line 303) to `_parse_chatgpt_rollout`, keeping its existing signature and body unchanged. Then add the new public entry point:

```python
def parse_chatgpt(
    sessions_dir: Path,
    stale_after_minutes: int = 30,
    now: Optional[datetime] = None,
    candidate_file_count: int = DEFAULT_CANDIDATE_FILE_COUNT,
    enable_live: bool = False,
    live_cache: Optional[LiveSourceCache] = None,
    cli_path: str = "codex",
    cli_timeout_seconds: int = 20,
    live_reader: Optional[Callable[[str, int], Dict[str, Any]]] = None,
) -> ProviderState:
    if not enable_live or live_cache is None:
        return _parse_chatgpt_rollout(
            sessions_dir,
            stale_after_minutes,
            now,
            candidate_file_count,
        )

    reader = live_reader or read_account_rate_limits
    current = now or datetime.now(timezone.utc)
    if live_cache.should_attempt(current):
        try:
            payload = reader(cli_path, cli_timeout_seconds)
            live_cache.record_success(payload, current)
        except CodexCliError as exc:
            live_cache.record_failure(exc, current)
        except Exception as exc:  # noqa: BLE001 - never let the poller die
            live_cache.record_failure(exc, current)

    payload = live_cache.payload
    if payload is None:
        # Never succeeded: a rollout reading beats no reading at all.
        return _parse_chatgpt_rollout(
            sessions_dir,
            stale_after_minutes,
            now,
            candidate_file_count,
        )

    rate_limits = payload.get("rateLimits")
    if not isinstance(rate_limits, dict):
        return _parse_chatgpt_rollout(
            sessions_dir,
            stale_after_minutes,
            now,
            candidate_file_count,
        )

    local_usage, model_usage = _chatgpt_usage(Path(sessions_dir), current)
    backed_off = live_cache.is_backed_off(current)
    return ProviderState(
        key="chatgpt",
        label="ChatGPT",
        mode="oauth",
        meters=_live_meters(rate_limits),
        credits=_live_credits(rate_limits),
        plan_type=rate_limits.get("planType"),
        error=None,
        last_updated=_iso_datetime(live_cache.fetched_at)
        if live_cache.fetched_at is not None
        else None,
        oauth_backed_off=backed_off,
        oauth_backoff_reason=live_cache.backoff_reason if backed_off else None,
        oauth_cache_age_seconds=live_cache.age_seconds(current),
        oauth_next_retry_at=_iso_datetime(live_cache.next_retry_at)
        if backed_off and live_cache.next_retry_at is not None
        else None,
        local_usage=local_usage,
        model_usage=model_usage,
    )
```

Add the supporting import and helper at the top of the module:

```python
from typing import Callable  # add to the existing typing import line

from app.providers.codex_cli import CodexCliError, read_account_rate_limits
from app.providers.live_cache import LiveSourceCache


def _iso_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
```

`app/providers/live_cache.py` imports nothing from `chatgpt.py`, so this import
introduces no cycle.

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — 79 passed. Every pre-existing ChatGPT test must stay green untouched, since `enable_live` defaults to `False`.

- [ ] **Step 8: Commit**

```bash
git add app/providers/chatgpt.py tests/test_providers.py tests/fixtures/chatgpt/account-rate-limits.json
git commit -m "Add live ChatGPT quota path with rollout fallback"
```

---

### Task 6: Wire the live source into the poller

**Files:**
- Modify: `app/poller.py:39-71`
- Test: `tests/test_poller.py`

**Interfaces:**
- Consumes: `Settings.enable_chatgpt_live`, `.chatgpt_live_min_interval_seconds`, `.codex_cli_path`, `.codex_cli_timeout_seconds` (Task 4); `LiveSourceCache` (Task 2); `parse_chatgpt` keyword arguments (Task 5).
- Produces: `UsagePoller._chatgpt_live_cache`, persisted across polls.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_poller.py`:

```python
@pytest.mark.asyncio
async def test_poller_passes_live_settings_to_chatgpt(monkeypatch, tmp_path):
    captured = {}

    def fake_chatgpt(*args, **kwargs):
        captured.update(kwargs)
        return ProviderState(key="chatgpt", label="ChatGPT", mode="local")

    async def working_claude(*args, **kwargs):
        return ProviderState(key="claude", label="Claude", mode="local")

    monkeypatch.setattr("app.poller.parse_chatgpt", fake_chatgpt)
    monkeypatch.setattr("app.poller.parse_claude", working_claude)
    settings = poller_settings(
        tmp_path,
        enable_chatgpt_live=True,
        codex_cli_path="/fake/codex",
        codex_cli_timeout_seconds=9,
    )
    database = Database(settings.database_path)
    alerts = AlertEngine(database, NullNotifier())
    poller = UsagePoller(settings, database, alerts)

    await poller.refresh()

    assert captured["enable_live"] is True
    assert captured["cli_path"] == "/fake/codex"
    assert captured["cli_timeout_seconds"] == 9
    assert captured["live_cache"] is poller._chatgpt_live_cache


@pytest.mark.asyncio
async def test_live_cache_survives_across_polls(monkeypatch, tmp_path):
    seen = []

    def fake_chatgpt(*args, **kwargs):
        seen.append(kwargs["live_cache"])
        return ProviderState(key="chatgpt", label="ChatGPT", mode="local")

    async def working_claude(*args, **kwargs):
        return ProviderState(key="claude", label="Claude", mode="local")

    monkeypatch.setattr("app.poller.parse_chatgpt", fake_chatgpt)
    monkeypatch.setattr("app.poller.parse_claude", working_claude)
    settings = poller_settings(tmp_path, enable_chatgpt_live=True)
    database = Database(settings.database_path)
    alerts = AlertEngine(database, NullNotifier())
    poller = UsagePoller(settings, database, alerts)

    await poller.refresh()
    await poller.refresh()

    assert seen[0] is seen[1], "cache must persist so backoff state is not lost"
```

This follows the existing `poller_settings` / `monkeypatch.setattr("app.poller.parse_chatgpt", ...)` pattern already used in `tests/test_poller.py:45-70`. The second test matters because a cache rebuilt per poll would silently defeat both the min-interval and the backoff.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_poller.py -k live_settings -v`
Expected: FAIL — `KeyError: 'enable_live'`, because the poller does not pass those arguments yet.

- [ ] **Step 3: Write minimal implementation**

In `app/poller.py`, import the cache:

```python
from app.providers.live_cache import LiveSourceCache
```

Add to `__init__` after `_claude_oauth_cache` (line 39-42):

```python
        self._chatgpt_live_cache = LiveSourceCache(
            "Codex CLI",
            settings.chatgpt_live_min_interval_seconds,
            settings.oauth_max_backoff_seconds,
        )
```

Extend the `parse_chatgpt` call (lines 56-61):

```python
                    chatgpt = await asyncio.to_thread(
                        parse_chatgpt,
                        self.settings.codex_sessions_dir,
                        self.settings.stale_after_minutes,
                        candidate_file_count=self.settings.chatgpt_candidate_files,
                        enable_live=self.settings.enable_chatgpt_live,
                        live_cache=self._chatgpt_live_cache,
                        cli_path=self.settings.codex_cli_path,
                        cli_timeout_seconds=self.settings.codex_cli_timeout_seconds,
                    )
```

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — 81 passed.

- [ ] **Step 5: Commit**

```bash
git add app/poller.py tests/test_poller.py
git commit -m "Wire ChatGPT live source into the poller"
```

---

### Task 7: Show live and backoff state for both providers in the UI

The OAuth backoff banner and the local-only note are both hardcoded to Claude, so a ChatGPT card in live mode would render neither.

**Files:**
- Modify: `frontend/src/App.tsx:11`, `:330-370`

**Interfaces:**
- Consumes: `source: "app-server"` and `mode: "oauth"` on the ChatGPT provider (Task 5).
- Produces: no new exports.

- [ ] **Step 1: Widen the source union**

`frontend/src/App.tsx:11`:

```tsx
  source: "rollout" | "oauth" | "local" | "app-server";
```

- [ ] **Step 2: De-hardcode the OAuth cache-age chip**

Replace line 330's condition so it applies to any provider reporting a cache age:

```tsx
          {oauthCacheAge !== null && (
            <span>Live reading {durationLabel(oauthCacheAge)}</span>
          )}
```

- [ ] **Step 3: De-hardcode the backoff banner**

Replace the `provider.key === "claude" && provider.oauth_backed_off` guard at line 336 with:

```tsx
      {provider.oauth_backed_off && (
        <div className="oauth-status" role="status">
          <strong>
            {provider.oauth_backoff_reason?.startsWith("Rate limited")
              ? "Live source rate limited"
              : "Live source retry delayed"}
          </strong>
          <p>
            {provider.oauth_backoff_reason}
            {oauthCacheAge !== null &&
              ` Cached reading is ${durationLabel(oauthCacheAge)}.`}
            {` Retrying at ${retryTime(provider.oauth_next_retry_at)}.`}
          </p>
        </div>
      )}
```

- [ ] **Step 4: Give ChatGPT its own local-only note**

Replace the Claude-only block at lines 359-370 with one that serves both providers:

```tsx
      {provider.mode === "local" && !provider.oauth_backed_off && (
        <div className="local-note">
          <strong>Local-only mode</strong>
          {provider.key === "claude" ? (
            <p>
              Claude’s local records provide token and cost estimates, not quota
              percentages. Set <code>ENABLE_CLAUDE_OAUTH=1</code> before starting
              the app to opt into the fragile live-percentage source.
            </p>
          ) : (
            <p>
              ChatGPT percentages come from Codex session files on disk, so they
              are only as fresh as your last Codex session. Set{" "}
              <code>ENABLE_CHATGPT_LIVE=1</code> before starting the app to read
              live figures through the Codex CLI.
            </p>
          )}
        </div>
      )}
```

- [ ] **Step 5: Typecheck and build**

Run: `cd frontend && npx tsc --noEmit && npm run build`
Expected: both exit 0 with no errors.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/App.tsx
git commit -m "Show live-source state for both providers"
```

---

### Task 8: Document the live source

**Files:**
- Modify: `README.md:63-78`, and a new section after the Claude OAuth section (`README.md:86-111`)

**Interfaces:**
- Consumes: setting names from Task 4.
- Produces: no code.

- [ ] **Step 1: Add the environment variables to the table**

Insert into the table at `README.md:63-78`, keeping alphabetical-ish grouping with the other ChatGPT rows:

```markdown
| `ENABLE_CHATGPT_LIVE` | `0` | Opt into live ChatGPT quota via the Codex CLI. |
| `CHATGPT_LIVE_MIN_INTERVAL_SECONDS` | `300` | Minimum interval between Codex CLI usage reads. |
| `CODEX_CLI_PATH` | `codex` | Path to the Codex CLI executable. |
| `CODEX_CLI_TIMEOUT_SECONDS` | `20` | Timeout for a single Codex CLI read. |
```

- [ ] **Step 2: Add the explanatory section**

Add after the Claude OAuth section:

```markdown
## ChatGPT live quota opt-in

Live ChatGPT percentages are **off by default**. When enabled, the backend runs
`codex app-server` and calls its `account/rateLimits/read` JSON-RPC method,
caching the result for at least five minutes by default:

```bash
ENABLE_CHATGPT_LIVE=1 ./run.sh
```

Quota Glass never reads `~/.codex/auth.json` and never handles Codex tokens.
Authentication, token refresh, and request attestation stay inside the Codex
CLI. This does mean the feature depends on the CLI being installed and logged
in, and on `app-server` — an experimental interface — keeping its current shape.

Any failure (CLI missing, logged out, timeout, or a protocol change) falls back
to parsing Codex session files on disk, which is the default behaviour. A
previously successful reading stays visible as a cached value during backoff.
With `ENABLE_CHATGPT_LIVE=0`, no subprocess is spawned and no network call is
made.
```

- [ ] **Step 3: Verify the documented default matches the code**

Run: `.venv/bin/python -c "from app.settings import Settings; s = Settings(); print(s.enable_chatgpt_live, s.chatgpt_live_min_interval_seconds, s.codex_cli_path, s.codex_cli_timeout_seconds)"`
Expected: `False 300 codex 20` — matching the table exactly.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "Document the ChatGPT live quota opt-in"
```

---

### Task 9: End-to-end verification against the real CLI

Everything above is offline. This task confirms the feature works against the actual Codex CLI on this machine.

**Files:** none modified.

**Interfaces:** consumes the finished feature.

- [ ] **Step 1: Confirm the full suite passes**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — 81 passed.

- [ ] **Step 2: Read live limits through the real CLI**

Run:

```bash
.venv/bin/python -c "
from app.providers.codex_cli import read_account_rate_limits
r = read_account_rate_limits()
print('usedPercent:', r['rateLimits']['primary']['usedPercent'])
print('planType:', r['rateLimits']['planType'])
"
```

Expected: a percentage and a plan type. If this raises `CodexCliUnavailable`, run `codex login` first — that is a genuine unavailability, not a defect.

- [ ] **Step 3: Restart the app with the flag on**

Run: `kill $(pgrep -f "bash ./run.sh")` then `ENABLE_CHATGPT_LIVE=1 ENABLE_CLAUDE_OAUTH=1 ./run.sh`

- [ ] **Step 4: Confirm the API reports live mode**

Run:

```bash
curl -s http://127.0.0.1:5173/api/state | python3 -c "
import json, sys
state = json.load(sys.stdin)
for provider in state['providers']:
    print(provider['key'], provider['mode'],
          [(m['key'], m['used_pct'], m['source'], m['stale']) for m in provider['meters']])
"
```

Expected: the `chatgpt` provider reports `mode=oauth`, meter key `chatgpt.primary`, `source=app-server`, `stale=False`, and a percentage matching Step 2. Requesting through port 5173 (not 8000) also confirms the Vite proxy path the browser uses.

- [ ] **Step 5: Confirm fallback still works**

Run: `kill $(pgrep -f "bash ./run.sh")` then `ENABLE_CHATGPT_LIVE=1 CODEX_CLI_PATH=/nonexistent ./run.sh`, and re-run the Step 4 command.

Expected: the `chatgpt` provider reports `mode=local` with `source=rollout` meters — degraded, not broken. Restore the normal command afterwards.
