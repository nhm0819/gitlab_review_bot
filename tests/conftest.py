"""Shared fixtures: local HTTP servers standing in for GitLab and vLLM."""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

CI_ENV_PREFIXES = ("VLLM_", "CI_", "GITLAB_", "DESCRIBE_", "MAX_", "POST_",
                   "REVIEW_BOT_", "LOG_", "LOKI_")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Stop a test's environment from leaking into the next one."""
    for key in list(os.environ):
        if key.startswith(CI_ENV_PREFIXES):
            monkeypatch.delenv(key, raising=False)
    yield


def serve(handler_cls):
    """Start an HTTP server on a free port; returns (base_url, server)."""
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}", server


def json_response(handler, obj, status=200):
    body = json.dumps(obj).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def read_json(handler):
    length = int(handler.headers.get("Content-Length", 0))
    return json.loads(handler.rfile.read(length)) if length else {}


def chat_completion(model, answer, thinking=True):
    """An OpenAI-shaped response, optionally wrapped in a Qwen think block."""
    content = json.dumps(answer)
    if thinking:
        content = "<think>\nreasoning\n</think>\n\n" + content
    return {
        "id": "chatcmpl-test", "object": "chat.completion", "model": model,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": content}}],
    }


class RecordingHandler(BaseHTTPRequestHandler):
    """Base handler that silences logging and records requests on the class."""

    requests: list = []

    def log_message(self, *args):
        pass
