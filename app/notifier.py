import subprocess
from typing import Protocol


class NotificationError(RuntimeError):
    pass


def applescript_string(value: str) -> str:
    escaped = (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )
    return '"%s"' % escaped


def build_notification_script(title: str, subtitle: str, message: str) -> str:
    return "display notification %s with title %s subtitle %s" % (
        applescript_string(message),
        applescript_string(title),
        applescript_string(subtitle),
    )


class Notifier(Protocol):
    def notify(self, title: str, subtitle: str, message: str) -> None:
        ...


class MacOSNotifier:
    def notify(self, title: str, subtitle: str, message: str) -> None:
        script = build_notification_script(title, subtitle, message)
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            detail = stderr or "osascript exited with status %s" % result.returncode
            raise NotificationError(detail)


class NullNotifier:
    def notify(self, title: str, subtitle: str, message: str) -> None:
        return None
