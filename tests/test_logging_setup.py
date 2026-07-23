"""Structured logging, secret redaction, and the Loki push path."""
import json

import pytest

from review_bot.logging_setup import redact, setup_logging, shutdown_logging

from conftest import RecordingHandler, serve


@pytest.mark.parametrize("text,leak", [
    ("token is glpat-AbCdEf123456789xyz here", "glpat-AbCdEf123456789xyz"),
    ("github_pat_11AQQEUHY03FehGPPoxp0x_orHFwRWKZ3IfWmXDm", "github_pat_11AQQ"),
    ('{"PRIVATE-TOKEN": "supersecretvalue"}', "supersecretvalue"),
    ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9abc", "eyJhbGciOiJIUzI1NiJ9abc"),
    ("password=hunter2000", "hunter2000"),
])
def test_credentials_are_redacted(text, leak):
    assert leak not in redact(text)


def test_ordinary_text_is_untouched():
    assert redact("normal message about src/main.py") == "normal message about src/main.py"


def test_json_line_carries_ci_context_and_fields(monkeypatch, capsys):
    monkeypatch.setenv("CI_PROJECT_PATH", "kodata/backend")
    monkeypatch.setenv("CI_MERGE_REQUEST_IID", "7")
    monkeypatch.setenv("LOG_FORMAT", "json")
    log = setup_logging("review")
    log.info("planned review of %d file(s)", 3, extra={"fields": {"files": 3}})
    shutdown_logging()
    line = json.loads(capsys.readouterr().out.strip())
    assert line["msg"] == "planned review of 3 file(s)"
    assert line["component"] == "review"
    assert line["project_path"] == "kodata/backend"
    assert line["mr_iid"] == "7"
    assert line["files"] == 3


def test_secrets_are_redacted_before_reaching_stdout(monkeypatch, capsys):
    monkeypatch.setenv("LOG_FORMAT", "json")
    log = setup_logging("review")
    log.info("cloning with glpat-SHOULDNEVERAPPEAR12345")
    shutdown_logging()
    assert "SHOULDNEVERAPPEAR" not in capsys.readouterr().out


def test_text_format(monkeypatch, capsys):
    monkeypatch.setenv("LOG_FORMAT", "text")
    log = setup_logging("review")
    log.info("reviewing batch %d/%d", 2, 5, extra={"fields": {"files": 4}})
    shutdown_logging()
    assert capsys.readouterr().out.strip() == "[info] reviewing batch 2/5 files=4"


def test_loki_push_uses_labels_fields_and_tenant(monkeypatch):
    pushed = []

    class Handler(RecordingHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            pushed.append({"body": json.loads(self.rfile.read(length)),
                           "headers": dict(self.headers)})
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

    url, _ = serve(Handler)
    monkeypatch.setenv("CI_PROJECT_PATH", "kodata/backend")
    monkeypatch.setenv("CI_MERGE_REQUEST_IID", "7")
    monkeypatch.setenv("LOKI_URL", url)
    monkeypatch.setenv("LOKI_TENANT", "kodata")
    monkeypatch.setenv("LOKI_EXTRA_LABELS", "env=prod")

    log = setup_logging("describe")
    log.info("summarizing batch %d/%d", 1, 3, extra={"fields": {"batch": 1}})
    log.warning("note")
    shutdown_logging()

    stream = pushed[0]["body"]["streams"][0]
    assert stream["stream"]["component"] == "describe"
    assert stream["stream"]["project"] == "kodata/backend"
    assert stream["stream"]["env"] == "prod"
    assert pushed[0]["headers"]["X-Scope-OrgID"] == "kodata"
    assert len(stream["values"]) == 2

    timestamp, payload = stream["values"][0]
    assert timestamp.isdigit() and len(timestamp) == 19, "Loki expects unix nanoseconds"
    parsed = json.loads(payload)
    assert parsed["batch"] == 1
    # High-cardinality identifiers must stay out of the label set.
    assert "mr_iid" in parsed and "mr_iid" not in stream["stream"]


def test_unreachable_loki_does_not_break_the_job(monkeypatch, capsys):
    monkeypatch.setenv("LOKI_URL", "http://127.0.0.1:9/dead")
    monkeypatch.setenv("LOG_FORMAT", "json")
    log = setup_logging("review")
    log.info("this still has to reach stdout")
    shutdown_logging()
    captured = capsys.readouterr()
    assert "this still has to reach stdout" in captured.out
    assert "Loki push failed" in captured.err
