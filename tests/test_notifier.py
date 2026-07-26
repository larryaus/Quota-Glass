import subprocess

import pytest

from app.notifier import (
    MacOSNotifier,
    NotificationError,
    applescript_string,
    build_notification_script,
)


def test_applescript_string_escapes_quotes_slashes_and_newlines():
    assert applescript_string('a "quote" \\ path\nnext') == (
        '"a \\"quote\\" \\\\ path\\nnext"'
    )


def test_notification_script_escapes_every_interpolated_field():
    script = build_notification_script(
        'Title "quoted"',
        "Sub\\title",
        'Message "x"\\y',
    )
    assert script == (
        'display notification "Message \\"x\\"\\\\y" '
        'with title "Title \\"quoted\\"" subtitle "Sub\\\\title"'
    )


def test_macos_notifier_passes_script_as_argument_without_shell(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("app.notifier.subprocess.run", fake_run)
    MacOSNotifier().notify('T"', "S\\", 'M"\n')
    assert captured["args"][0:2] == ["/usr/bin/osascript", "-e"]
    assert captured["args"][2] == (
        'display notification "M\\"\\n" with title "T\\"" subtitle "S\\\\"'
    )
    assert "shell" not in captured["kwargs"]
    assert captured["kwargs"]["text"] is True


def test_macos_notifier_raises_with_stderr_on_nonzero_exit(monkeypatch):
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", "notifications denied\n")

    monkeypatch.setattr("app.notifier.subprocess.run", fake_run)

    with pytest.raises(NotificationError, match="notifications denied"):
        MacOSNotifier().notify("Title", "Subtitle", "Message")
