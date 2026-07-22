"""Structured logging for the bot.

Two delivery paths, because CI job pods are ephemeral and how their output is
captured depends on the runner's execution strategy:

1. stdout (always) - JSON lines. With the Kubernetes executor's default
   `attach` strategy the job's output is also the build container's stdout,
   so an Alloy/Promtail pod-log scrape picks it up. Under the legacy
   `exec` strategy (FF_USE_LEGACY_KUBERNETES_EXECUTION_STRATEGY=true) it is
   NOT written to the container's stdout, and only the GitLab job trace has it.
2. Loki push (opt-in) - set LOKI_URL to push directly to Loki's HTTP API.
   This does not depend on the runner's strategy at all.

Label cardinality: only low-cardinality values become Loki stream labels.
Per-job identifiers (merge request iid, pipeline id, commit sha) are fields
inside the JSON line instead, so they do not multiply the stream count.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

SERVICE_NAME = "gitlab-review-bot"

_SECRET_PATTERNS: Tuple[re.Pattern, ...] = (
    re.compile(r"glpat-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"gl(?:drt|cbt|ptt|soat)-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"),
)

_KV_SECRET_RE = re.compile(
    r"(?i)\b(authorization|private[-_]?token|api[-_]?key|token|password|secret)"
    r"(\"?\s*[:=]\s*\"?)([^\s\"',}&]{4,})"
)

_LOKI_HANDLER: Optional["LokiHandler"] = None


def redact(text: str) -> str:
    """Strip anything that looks like a credential before it leaves the process."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return _KV_SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)


def _ci_context() -> Dict[str, Any]:
    """Per-job identifiers, kept as log fields rather than Loki labels."""
    mapping = {
        "project_id": "CI_PROJECT_ID",
        "project_path": "CI_PROJECT_PATH",
        "mr_iid": "CI_MERGE_REQUEST_IID",
        "pipeline_id": "CI_PIPELINE_ID",
        "job_id": "CI_JOB_ID",
        "job_name": "CI_JOB_NAME",
        "job_url": "CI_JOB_URL",
        "ref": "CI_COMMIT_REF_NAME",
        "commit_sha": "CI_COMMIT_SHORT_SHA",
    }
    return {key: os.environ[env] for key, env in mapping.items() if os.environ.get(env)}


def _loki_labels(component: str) -> Dict[str, str]:
    labels = {
        "service_name": SERVICE_NAME,
        "job": SERVICE_NAME,
        "component": component,
    }
    project = os.environ.get("CI_PROJECT_PATH")
    if project:
        labels["project"] = project
    for pair in (os.environ.get("LOKI_EXTRA_LABELS") or "").split(","):
        if "=" in pair:
            key, value = pair.split("=", 1)
            if key.strip():
                labels[key.strip()] = value.strip()
    return labels


class JsonFormatter(logging.Formatter):
    def __init__(self, context: Dict[str, Any]):
        super().__init__()
        self._context = context

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        payload.update(self._context)
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["error"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = f"[{record.levelname.lower()}] {redact(record.getMessage())}"
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict) and extra:
            line += " " + " ".join(f"{k}={v}" for k, v in extra.items())
        if record.exc_info:
            line += "\n" + redact(self.formatException(record.exc_info))
        return line


class LokiHandler(logging.Handler):
    """Buffers records and pushes them to Loki once at the end of the job.

    Logging must never break the job, so every failure here is swallowed after
    a single warning on stderr.
    """

    MAX_BUFFER = 500

    def __init__(self, url: str, labels: Dict[str, str], tenant: Optional[str], timeout: int):
        super().__init__()
        self._url = url.rstrip("/") + "/loki/api/v1/push"
        self._labels = labels
        self._headers = {"Content-Type": "application/json"}
        if tenant:
            self._headers["X-Scope-OrgID"] = tenant
        self._timeout = timeout
        self._entries: List[Tuple[str, str]] = []
        self._failed = False

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._entries.append((str(int(record.created * 1_000_000_000)), self.format(record)))
        except Exception:
            return
        if len(self._entries) >= self.MAX_BUFFER:
            self.flush()

    def flush(self) -> None:
        if not self._entries or self._failed:
            self._entries.clear()
            return
        payload = {"streams": [{"stream": self._labels, "values": [list(e) for e in self._entries]}]}
        try:
            resp = requests.post(self._url, headers=self._headers, json=payload, timeout=self._timeout)
            if resp.status_code >= 400:
                self._failed = True
                print(f"[logging] Loki push failed: HTTP {resp.status_code}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 - logging must not break the job
            self._failed = True
            print(f"[logging] Loki push failed: {type(exc).__name__}", file=sys.stderr)
        finally:
            self._entries.clear()


def setup_logging(component: str) -> logging.Logger:
    """Configure root logging for one CLI run. Reads env directly, not Config."""
    global _LOKI_HANDLER

    level = (os.environ.get("LOG_LEVEL") or "INFO").upper()
    fmt = (os.environ.get("LOG_FORMAT") or "json").lower()
    context = {"service": SERVICE_NAME, "component": component, **_ci_context()}

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level, logging.INFO))

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(JsonFormatter(context) if fmt == "json" else TextFormatter())
    root.addHandler(stream)

    loki_url = os.environ.get("LOKI_URL")
    if loki_url:
        handler = LokiHandler(
            url=loki_url,
            labels=_loki_labels(component),
            tenant=os.environ.get("LOKI_TENANT"),
            timeout=int(os.environ.get("LOKI_TIMEOUT", "10")),
        )
        # Loki always receives JSON, regardless of the stdout format.
        handler.setFormatter(JsonFormatter(context))
        root.addHandler(handler)
        _LOKI_HANDLER = handler

    # The OpenAI/urllib3 clients are chatty at DEBUG and can echo request bodies.
    for noisy in ("httpx", "httpcore", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger(f"review_bot.{component}")


def shutdown_logging() -> None:
    """Flush the Loki buffer. Safe to call even when Loki was never configured."""
    global _LOKI_HANDLER
    if _LOKI_HANDLER is not None:
        _LOKI_HANDLER.flush()
        _LOKI_HANDLER = None
    logging.shutdown()
