import smtplib
import ssl
import subprocess
from email.message import EmailMessage
from typing import List, Protocol, Sequence


class NotificationError(RuntimeError):
    pass


class NotificationConfigurationError(ValueError):
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


class SmtpEmailNotifier:
    def __init__(
        self,
        host: str,
        port: int,
        sender: str,
        recipients: str,
        username: str = "",
        password: str = "",
        security: str = "starttls",
        timeout_seconds: int = 10,
    ) -> None:
        self.host = host.strip()
        self.port = max(1, int(port))
        self.sender = sender.strip()
        self.recipients = self._parse_recipients(recipients)
        self.username = username.strip()
        self.password = password
        self.security = security.strip().lower()
        self.timeout_seconds = max(1, int(timeout_seconds))
        self._validate()

    @staticmethod
    def _parse_recipients(value: str) -> List[str]:
        if "\r" in value or "\n" in value:
            raise NotificationConfigurationError(
                "EMAIL_TO cannot contain newlines."
            )
        recipients = [
            recipient.strip()
            for recipient in value.split(",")
            if recipient.strip()
        ]
        if not recipients:
            raise NotificationConfigurationError(
                "EMAIL_TO is required when email notifications are enabled."
            )
        return recipients

    def _validate(self) -> None:
        if not self.host:
            raise NotificationConfigurationError(
                "SMTP_HOST is required when email notifications are enabled."
            )
        if not self.sender:
            raise NotificationConfigurationError(
                "EMAIL_FROM or SMTP_USERNAME is required when email "
                "notifications are enabled."
            )
        if "\r" in self.sender or "\n" in self.sender:
            raise NotificationConfigurationError(
                "EMAIL_FROM cannot contain newlines."
            )
        if self.security not in ("starttls", "ssl", "none"):
            raise NotificationConfigurationError(
                "SMTP_SECURITY must be one of: starttls, ssl, none."
            )
        if bool(self.username) != bool(self.password):
            raise NotificationConfigurationError(
                "SMTP_USERNAME and SMTP_PASSWORD must be set together."
            )

    def notify(self, title: str, subtitle: str, message: str) -> None:
        email = EmailMessage()
        email["Subject"] = "[Quota Glass] %s - %s" % (title, subtitle)
        email["From"] = self.sender
        email["To"] = ", ".join(self.recipients)
        email.set_content(
            "%s\n\n%s\n\n%s\n" % (title, subtitle, message)
        )

        try:
            if self.security == "ssl":
                with smtplib.SMTP_SSL(
                    self.host,
                    self.port,
                    timeout=self.timeout_seconds,
                    context=ssl.create_default_context(),
                ) as client:
                    self._send(client, email)
                return

            with smtplib.SMTP(
                self.host,
                self.port,
                timeout=self.timeout_seconds,
            ) as client:
                if self.security == "starttls":
                    client.ehlo()
                    client.starttls(context=ssl.create_default_context())
                    client.ehlo()
                self._send(client, email)
        except NotificationError:
            raise
        except Exception as exc:
            raise NotificationError("SMTP delivery failed: %s" % exc) from exc

    def _send(self, client: smtplib.SMTP, email: EmailMessage) -> None:
        if self.username:
            client.login(self.username, self.password)
        client.send_message(
            email,
            from_addr=self.sender,
            to_addrs=self.recipients,
        )


class CompositeNotifier:
    def __init__(self, notifiers: Sequence[Notifier]) -> None:
        self.notifiers = list(notifiers)

    def notify(self, title: str, subtitle: str, message: str) -> None:
        errors: List[str] = []
        for notifier in self.notifiers:
            try:
                notifier.notify(title, subtitle, message)
            except Exception as exc:
                errors.append(
                    "%s: %s" % (notifier.__class__.__name__, exc)
                )
        if errors:
            raise NotificationError("; ".join(errors))


class NullNotifier:
    def notify(self, title: str, subtitle: str, message: str) -> None:
        return None
