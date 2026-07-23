"""Retry behaviour, including the guarantee that comments are never double-posted."""
import pytest
import requests

from review_bot import retry as retry_module
from review_bot.retry import request_with_retry

from conftest import RecordingHandler, json_response, serve


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    monkeypatch.setattr(retry_module.time, "sleep", lambda _s: None)


def make_server(script):
    """script: list of status codes to return, one per request."""
    calls = []

    class Handler(RecordingHandler):
        def _respond(self):
            index = len(calls)
            calls.append(self.command)
            status = script[min(index, len(script) - 1)]
            json_response(self, {"ok": status < 400}, status=status)

        do_GET = do_POST = do_PUT = _respond

    url, server = serve(Handler)
    return url, calls, server


def test_retries_transient_error_then_succeeds():
    url, calls, _ = make_server([502, 502, 200])
    with requests.Session() as s:
        resp = request_with_retry(s, "GET", url, idempotent=True, attempts=4)
    assert resp.status_code == 200
    assert len(calls) == 3


def test_gives_up_and_returns_last_response():
    url, calls, _ = make_server([500])
    with requests.Session() as s:
        resp = request_with_retry(s, "GET", url, idempotent=True, attempts=3)
    assert resp.status_code == 500
    assert len(calls) == 3


def test_client_errors_are_not_retried():
    url, calls, _ = make_server([404])
    with requests.Session() as s:
        resp = request_with_retry(s, "GET", url, idempotent=True, attempts=3)
    assert resp.status_code == 404
    assert len(calls) == 1


def test_non_idempotent_request_is_not_replayed_on_500():
    """A 500 may mean the note was created but the response was lost."""
    url, calls, _ = make_server([500])
    with requests.Session() as s:
        resp = request_with_retry(s, "POST", url, idempotent=False, attempts=3)
    assert resp.status_code == 500
    assert len(calls) == 1, "replaying this POST could post a duplicate comment"


def test_non_idempotent_request_is_replayed_when_server_refused():
    """429/503 mean the request was rejected outright, so it is safe to resend."""
    url, calls, _ = make_server([503, 200])
    with requests.Session() as s:
        resp = request_with_retry(s, "POST", url, idempotent=False, attempts=3)
    assert resp.status_code == 200
    assert len(calls) == 2


def test_connection_error_is_retried_then_raises():
    """Nothing reached the server, so replaying is safe even for a POST."""
    attempts = {"n": 0}

    class FailingSession(requests.Session):
        def request(self, *a, **kw):
            attempts["n"] += 1
            raise requests.ConnectionError("refused")

    with FailingSession() as s:
        with pytest.raises(requests.ConnectionError):
            request_with_retry(s, "POST", "http://127.0.0.1:1/x", idempotent=False, attempts=3)
    assert attempts["n"] == 3


def test_read_timeout_on_non_idempotent_request_is_not_replayed():
    attempts = {"n": 0}

    class TimingOutSession(requests.Session):
        def request(self, *a, **kw):
            attempts["n"] += 1
            raise requests.Timeout("read timed out")

    with TimingOutSession() as s:
        with pytest.raises(requests.Timeout):
            request_with_retry(s, "POST", "http://127.0.0.1:1/x", idempotent=False, attempts=3)
    assert attempts["n"] == 1, "the server may already have applied this request"


def test_retry_after_header_is_respected(monkeypatch):
    slept = []
    monkeypatch.setattr(retry_module.time, "sleep", slept.append)

    class Handler(RecordingHandler):
        seen = []

        def do_GET(self):
            Handler.seen.append(1)
            if len(Handler.seen) == 1:
                self.send_response(429)
                self.send_header("Retry-After", "7")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                json_response(self, {"ok": True})

    url, _ = serve(Handler)
    with requests.Session() as s:
        assert request_with_retry(s, "GET", url, idempotent=True, attempts=3).status_code == 200
    assert slept == [7.0]
