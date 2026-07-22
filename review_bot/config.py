"""Configuration loading for the GitLab review bot.

All configuration comes from environment variables so the bot can run as a
plain GitLab CI/CD job with no extra files required. GitLab's predefined
CI/CD variables (CI_SERVER_URL, CI_PROJECT_ID, CI_MERGE_REQUEST_IID,
CI_PROJECT_DIR) are used automatically when present.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _env(name: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    value = os.environ.get(name, default)
    if required and not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


@dataclass
class Config:
    gitlab_url: str
    gitlab_token: str
    project_id: str
    mr_iid: str

    vllm_base_url: str
    vllm_model: str
    vllm_api_key: str = "not-needed"
    vllm_timeout: int = 120
    vllm_max_tokens: int = 4096
    vllm_temperature: float = 0.2

    describe_language: str = "Korean"

    max_diff_chars: int = 60_000
    max_comments: int = 25
    post_inline_comments: bool = True
    post_summary_comment: bool = True

    config_path: Path = field(default_factory=lambda: Path(".gitlab/review-bot.yml"))
    project_dir: Path = field(default_factory=lambda: Path("."))

    @classmethod
    def from_env(cls) -> "Config":
        # Prefer explicit REVIEW_BOT_* / GITLAB_* vars, fall back to GitLab's
        # predefined CI/CD variables so the job works with zero extra config.
        gitlab_url = _env("GITLAB_URL") or _env("CI_SERVER_URL", required=True)
        project_id = _env("GITLAB_PROJECT_ID") or _env("CI_PROJECT_ID", required=True)
        mr_iid = _env("GITLAB_MERGE_REQUEST_IID") or _env("CI_MERGE_REQUEST_IID", required=True)
        gitlab_token = _env("REVIEW_BOT_GITLAB_TOKEN") or _env("GITLAB_TOKEN", required=True)
        # Internal vLLM (OpenAI-compatible) service.
        vllm_base_url = _env("VLLM_BASE_URL", required=True)
        vllm_model = _env("VLLM_MODEL", required=True)
        vllm_api_key = _env("VLLM_API_KEY", "not-needed")
        vllm_timeout = int(_env("VLLM_TIMEOUT", "120"))
        vllm_max_tokens = int(_env("VLLM_MAX_TOKENS", "4096"))
        vllm_temperature = float(_env("VLLM_TEMPERATURE", "0.2"))

        describe_language = _env("DESCRIBE_LANGUAGE", "Korean")

        max_diff_chars = int(_env("MAX_DIFF_CHARS", "60000"))
        max_comments = int(_env("MAX_COMMENTS", "25"))
        post_inline = (_env("POST_INLINE_COMMENTS", "true") or "true").lower() in ("1", "true", "yes")
        post_summary = (_env("POST_SUMMARY_COMMENT", "true") or "true").lower() in ("1", "true", "yes")

        config_path = Path(_env("REVIEW_BOT_CONFIG_PATH", ".gitlab/review-bot.yml"))
        project_dir = Path(_env("CI_PROJECT_DIR", "."))

        return cls(
            gitlab_url=gitlab_url.rstrip("/"),
            gitlab_token=gitlab_token,
            project_id=project_id,
            mr_iid=mr_iid,
            vllm_base_url=vllm_base_url.rstrip("/"),
            vllm_model=vllm_model,
            vllm_api_key=vllm_api_key,
            vllm_timeout=vllm_timeout,
            vllm_max_tokens=vllm_max_tokens,
            vllm_temperature=vllm_temperature,
            describe_language=describe_language,
            max_diff_chars=max_diff_chars,
            max_comments=max_comments,
            post_inline_comments=post_inline,
            post_summary_comment=post_summary,
            config_path=config_path,
            project_dir=project_dir,
        )
