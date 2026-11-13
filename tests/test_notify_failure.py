"""Workflow notification transport: no network, no payload/credential logging."""

from contextlib import nullcontext
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs

import pytest

from scripts import notify_failure

ENV = {"TELEGRAM_BOT_TOKEN": "fake-token-do-not-log", "TELEGRAM_CHAT_ID": "-123&456"}


def test_request_is_one_form_encoded_post_with_original_text_and_timeout(capsys):
    calls = []

    def opener(request, *, timeout):
        calls.append((request, timeout))
        return nullcontext()

    assert notify_failure.notify("SYSTEM · Test failed", "Trigger: dispatch\nA & B + C",
                                 "https://example.test/run/1", env=ENV, opener=opener) == 0
    assert len(calls) == 1
    request, timeout = calls[0]
    assert request.full_url == f"https://api.telegram.org/bot{ENV['TELEGRAM_BOT_TOKEN']}/sendMessage"
    assert request.get_method() == "POST" and timeout == 15
    assert parse_qs(request.data.decode("utf-8")) == {
        "chat_id": [ENV["TELEGRAM_CHAT_ID"]],
        "text": ["SYSTEM · Test failed\nTrigger: dispatch\nA & B + C\nOpen run: https://example.test/run/1"],
    }
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("env", [{}, {"TELEGRAM_BOT_TOKEN": ENV["TELEGRAM_BOT_TOKEN"]},
                                  {"TELEGRAM_CHAT_ID": ENV["TELEGRAM_CHAT_ID"]}])
@pytest.mark.parametrize("strict", [False, True])
def test_missing_credentials_skip_without_opening_or_logging_values(env, strict, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("a missing credential must prevent sending")

    assert notify_failure.notify("title", env=env, opener=forbidden, strict=strict) == 0
    output = capsys.readouterr()
    assert output.out == "::notice::Telegram credentials absent; skipping notification\n"
    assert output.err == ""
    assert all(value not in output.out for value in ENV.values())


@pytest.mark.parametrize("error", [
    HTTPError("https://api.telegram.org/botfake-token-do-not-log/sendMessage", 403,
              "fake-token-do-not-log", {}, None),
    URLError("https://api.telegram.org/botfake-token-do-not-log/sendMessage"),
    TimeoutError("fake-token-do-not-log"),
    RuntimeError("fake-token-do-not-log"),
])
@pytest.mark.parametrize("strict", [False, True])
def test_send_errors_are_sanitized_and_only_strict_errors_are_fatal(error, strict, capsys):
    def opener(*args, **kwargs):
        raise error

    assert notify_failure.notify("private payload", env=ENV, opener=opener, strict=strict) == int(strict)
    output = capsys.readouterr()
    assert output.out == "::warning::Telegram notification delivery failed\n"
    assert output.err == ""
    for private in (*ENV.values(), "private payload", "api.telegram.org"):
        assert private not in output.out + output.err


def test_cli_preserves_preformatted_incident_message_and_deploy_timeout(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", ENV["TELEGRAM_BOT_TOKEN"])
    monkeypatch.setenv("TELEGRAM_CHAT_ID", ENV["TELEGRAM_CHAT_ID"])
    calls = []
    monkeypatch.setattr(notify_failure, "open_request", lambda req, **kw: calls.append((req, kw)) or nullcontext())
    message = "SYSTEM · Pipeline recovered\nOpen run: https://example.test/run/1"
    assert notify_failure.main(["--body", message, "--strict", "--timeout", "30"]) == 0
    assert parse_qs(calls[0][0].data.decode())["text"] == [message]
    assert calls[0][1] == {"timeout": 30}


def test_unfollowed_redirect_preserves_curl_delivery_boundary_without_retry(capsys):
    calls = []

    def opener(request, *, timeout):
        calls.append(request)
        raise HTTPError(request.full_url, 302, "redirect", {}, None)

    assert notify_failure.notify("title", env=ENV, opener=opener, strict=True) == 0
    assert len(calls) == 1
    assert notify_failure._NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://other.test") is None
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("timeout", ["0", "-1", "121", "nan", "inf"])
def test_cli_requires_a_bounded_positive_timeout(timeout):
    with pytest.raises(SystemExit) as exc:
        notify_failure.main([f"--timeout={timeout}"])
    assert exc.value.code == 2
