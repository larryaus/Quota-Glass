import subprocess

import pytest

from app.notifier import (
    CompositeNotifier,
    MacOSNotifier,
    NotificationConfigurationError,
    NotificationError,
    SmtpEmailNotifier,
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


class FakeSmtpClient:
    def __init__(self, host, port, **kwargs):
        self.host = host
        self.port = port
        self.kwargs = kwargs
        self.ehlo_calls = 0
        self.starttls_context = None
        self.login_args = None
        self.sent = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def ehlo(self):
        self.ehlo_calls += 1

    def starttls(self, context):
        self.starttls_context = context

    def login(self, username, password):
        self.login_args = (username, password)

    def send_message(self, message, from_addr, to_addrs):
        self.sent = (message, from_addr, to_addrs)


def test_smtp_notifier_sends_starttls_email(monkeypatch):
    clients = []
    tls_context = object()

    def smtp(host, port, **kwargs):
        client = FakeSmtpClient(host, port, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr("app.notifier.smtplib.SMTP", smtp)
    monkeypatch.setattr(
        "app.notifier.ssl.create_default_context",
        lambda: tls_context,
    )

    notifier = SmtpEmailNotifier(
        host="smtp.example.com",
        port=587,
        sender="quota@example.com",
        recipients="one@example.com, two@example.com",
        username="smtp-user",
        password="app-password",
    )
    notifier.notify(
        "Quota exhausted",
        "Primary limit",
        "Chatgpt reached 100% usage.",
    )

    assert len(clients) == 1
    client = clients[0]
    assert (client.host, client.port) == ("smtp.example.com", 587)
    assert client.kwargs["timeout"] == 10
    assert client.ehlo_calls == 2
    assert client.starttls_context is tls_context
    assert client.login_args == ("smtp-user", "app-password")
    email, sender, recipients = client.sent
    assert email["Subject"] == (
        "[Quota Glass] Quota exhausted - Primary limit"
    )
    assert email["From"] == "quota@example.com"
    assert email["To"] == "one@example.com, two@example.com"
    assert "Chatgpt reached 100% usage." in email.get_content()
    assert sender == "quota@example.com"
    assert recipients == ["one@example.com", "two@example.com"]


def test_smtp_notifier_uses_implicit_tls(monkeypatch):
    clients = []
    tls_context = object()

    def smtp_ssl(host, port, **kwargs):
        client = FakeSmtpClient(host, port, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr("app.notifier.smtplib.SMTP_SSL", smtp_ssl)
    monkeypatch.setattr(
        "app.notifier.ssl.create_default_context",
        lambda: tls_context,
    )

    SmtpEmailNotifier(
        host="smtp.example.com",
        port=465,
        sender="quota@example.com",
        recipients="owner@example.com",
        security="ssl",
    ).notify("Quota refreshed", "Weekly limit", "Usage is available again.")

    assert clients[0].kwargs["context"] is tls_context
    assert clients[0].ehlo_calls == 0
    assert clients[0].sent is not None


@pytest.mark.parametrize(
    "kwargs, message",
    [
        (
            {
                "host": "",
                "sender": "from@example.com",
                "recipients": "to@example.com",
            },
            "SMTP_HOST",
        ),
        (
            {
                "host": "smtp.example.com",
                "sender": "",
                "recipients": "to@example.com",
            },
            "EMAIL_FROM",
        ),
        (
            {
                "host": "smtp.example.com",
                "sender": "from@example.com",
                "recipients": "",
            },
            "EMAIL_TO",
        ),
        (
            {
                "host": "smtp.example.com",
                "sender": "from@example.com",
                "recipients": "to@example.com",
                "username": "user",
            },
            "must be set together",
        ),
    ],
)
def test_smtp_notifier_rejects_incomplete_configuration(kwargs, message):
    with pytest.raises(NotificationConfigurationError, match=message):
        SmtpEmailNotifier(port=587, **kwargs)


def test_smtp_notifier_wraps_delivery_errors(monkeypatch):
    def failing_smtp(host, port, **kwargs):
        raise OSError("mail server unavailable")

    monkeypatch.setattr("app.notifier.smtplib.SMTP", failing_smtp)
    notifier = SmtpEmailNotifier(
        host="smtp.example.com",
        port=587,
        sender="from@example.com",
        recipients="to@example.com",
    )

    with pytest.raises(
        NotificationError,
        match="SMTP delivery failed: mail server unavailable",
    ):
        notifier.notify("Title", "Subtitle", "Message")


class RecordingChildNotifier:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def notify(self, title, subtitle, message):
        self.calls.append((title, subtitle, message))
        if self.error is not None:
            raise self.error


def test_composite_notifier_attempts_every_channel_and_reports_failures():
    failing = RecordingChildNotifier(RuntimeError("desktop unavailable"))
    succeeding = RecordingChildNotifier()

    with pytest.raises(NotificationError, match="desktop unavailable"):
        CompositeNotifier([failing, succeeding]).notify(
            "Title",
            "Subtitle",
            "Message",
        )

    assert len(failing.calls) == 1
    assert len(succeeding.calls) == 1
