import asyncio
import logging

from fx30_mcp import notify
from fx30_mcp.notify import Limited, TwilioTexts, RecordedTexts

TOKEN = "tok-fake-123"
NUMBER = "+15550001111"

ENV = f"""# comment
TWILIO_ACCOUNT_SID="ACfake"
TWILIO_AUTH_TOKEN='{TOKEN}'
TWILIO_MESSAGING_SERVICE_SID=MGfake
MY_PHONE_NUMBER={NUMBER}
this line is not parseable {{ at all
"""


def _env(tmp_path, text=ENV):
    path = tmp_path / ".env"
    path.write_text(text)
    return path


def test_env_is_parsed_and_the_message_posted(tmp_path):
    calls = []

    def post(url, data, auth):
        calls.append((url, data, auth))
        return {}

    texts = TwilioTexts(_env(tmp_path), post=post)
    asyncio.run(texts.send("link", "hello there"))
    (url, data, auth), = calls
    assert url == "https://api.twilio.com/2010-04-01/Accounts/ACfake/Messages.json"
    assert data["To"] == NUMBER
    assert data["MessagingServiceSid"] == "MGfake"
    assert data["Body"] == "hello there"
    assert auth == ("ACfake", TOKEN)


def test_phone_number_is_the_from_without_a_messaging_service(tmp_path):
    text = ENV.replace("TWILIO_MESSAGING_SERVICE_SID=MGfake", "TWILIO_PHONE_NUMBER=+15559998888")
    calls = []
    texts = TwilioTexts(_env(tmp_path, text), post=lambda u, d, a: calls.append(d) or {})
    asyncio.run(texts.send("report", "x"))
    assert calls[0]["From"] == "+15559998888"
    assert "MessagingServiceSid" not in calls[0]


def test_a_send_failure_is_swallowed_and_logged_without_secrets(tmp_path, caplog):
    def post(url, data, auth):
        raise RuntimeError(f"boom {TOKEN} {NUMBER}")

    texts = TwilioTexts(_env(tmp_path), post=post)
    with caplog.at_level(logging.DEBUG):
        asyncio.run(texts.send("overheat", "hot"))
    assert "overheat" in caplog.text
    assert TOKEN not in caplog.text
    assert NUMBER not in caplog.text


def test_a_missing_env_file_is_swallowed(tmp_path):
    texts = TwilioTexts(tmp_path / "nope.env", post=lambda u, d, a: {})
    asyncio.run(texts.send("link", "x"))


def test_warnings_of_one_kind_are_limited():
    now = [0.0]
    inner = RecordedTexts()
    texts = Limited(inner, clock=lambda: now[0])

    async def go():
        await texts.send("overheat", "a")
        now[0] = 60.0
        await texts.send("overheat", "b")
        await texts.send("card", "c")
        now[0] = 601.0
        await texts.send("overheat", "d")

    asyncio.run(go())
    assert inner.sent == [("overheat", "a"), ("card", "c"), ("overheat", "d")]


def test_link_and_report_are_never_limited():
    now = [0.0]
    inner = RecordedTexts()
    texts = Limited(inner, clock=lambda: now[0])

    async def go():
        for kind in ("link", "report"):
            await texts.send(kind, "1")
            await texts.send(kind, "2")

    asyncio.run(go())
    assert len(inner.sent) == 4


def test_tests_never_build_the_real_sender(monkeypatch):
    monkeypatch.setenv("FX30_BACKEND", "sim")
    texts = notify.texts_for_server()
    assert isinstance(texts, Limited)
    assert isinstance(texts._texts, RecordedTexts)


def test_texts_off_uses_recorded(monkeypatch):
    monkeypatch.delenv("FX30_BACKEND", raising=False)
    monkeypatch.setenv("FX30_TEXTS", "off")
    assert isinstance(notify.texts_for_server()._texts, RecordedTexts)


def test_real_sender_when_the_credentials_file_exists(monkeypatch, tmp_path):
    env = tmp_path / "twilio.env"
    env.write_text(ENV)
    monkeypatch.delenv("FX30_BACKEND", raising=False)
    monkeypatch.delenv("FX30_TEXTS", raising=False)
    monkeypatch.setenv("FX30_TWILIO_ENV", str(env))
    sender = notify.texts_for_server()._texts
    assert isinstance(sender, TwilioTexts) and sender._env_path == env


def test_no_credentials_file_means_no_texts(monkeypatch, tmp_path):
    monkeypatch.delenv("FX30_BACKEND", raising=False)
    monkeypatch.delenv("FX30_TEXTS", raising=False)
    monkeypatch.setenv("FX30_TWILIO_ENV", str(tmp_path / "missing.env"))
    assert isinstance(notify.texts_for_server()._texts, RecordedTexts)


def test_messaging_service_sid_key_is_used(tmp_path):
    text = ENV.replace("TWILIO_MESSAGING_SERVICE_SID=MGfake", "MESSAGING_SERVICE_SID=MGplain")
    calls = []
    texts = TwilioTexts(_env(tmp_path, text), post=lambda u, d, a: calls.append(d) or {})
    asyncio.run(texts.send("link", "x"))
    assert calls[0]["MessagingServiceSid"] == "MGplain"


def test_a_repeated_key_takes_its_last_value(tmp_path):
    text = ENV + "MESSAGING_SERVICE_SID=MGfirst\nMESSAGING_SERVICE_SID=MGlast\n"
    calls = []
    texts = TwilioTexts(_env(tmp_path, text), post=lambda u, d, a: calls.append(d) or {})
    asyncio.run(texts.send("link", "x"))
    assert calls[0]["MessagingServiceSid"] == "MGlast"


def test_send_later_starts_at_once_and_does_not_wait(caplog):
    class Stuck:
        def __init__(self):
            self.sent = []

        async def send(self, kind, body):
            self.sent.append((kind, body))
            await asyncio.Event().wait()

    class Broken:
        async def send(self, kind, body):
            raise RuntimeError(f"no network {TOKEN}")

    async def go():
        pending: set = set()
        recorded, stuck = RecordedTexts(), Stuck()
        notify.send_later(recorded, "report", "Take 1", pending)
        notify.send_later(stuck, "link", "url", pending)
        assert recorded.sent == [("report", "Take 1")] and stuck.sent == [("link", "url")]
        assert len(pending) == 1  # the stuck one, kept so it is not collected mid-send
        with caplog.at_level(logging.WARNING):
            notify.send_later(Broken(), "stopped", "body", pending)
        assert "RuntimeError" in caplog.text and TOKEN not in caplog.text
        for task in pending:
            task.cancel()

    asyncio.run(go())
