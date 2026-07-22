"""Configuration loading for the GitLab review bot.

All configuration comes from environment variables so the bot can run as a
plain GitLab CI/CD job with no extra files required. GitLab's predefined
CI/CD variables (CI_SERVER_URL, CI_PROJECT_ID, CI_MERGE_REQUEST_IID,
CI_PROJECT_DIR) are used automatically when present.

Defaults target Qwen/Qwen3.6-35B-A3B-FP8 served by vLLM.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .llm import LLMSettings

# The model card's minimum recommended timeout for this deployment. Qwen3.6
# thinks before answering, so short timeouts cut off responses mid-reasoning.
MIN_TIMEOUT_SECONDS = 300


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _env(name: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    value = os.environ.get(name, default)
    if required and not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _env_bool(name: str, default: str) -> bool:
    return (_env(name, default) or default).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    gitlab_url: str
    gitlab_token: str
    project_id: str
    mr_iid: str

    llm: LLMSettings

    describe_language: str = "Korean"

    # Qwen3.6 has a 262,144-token context. `max_diff_chars` is the per-request
    # budget; larger changes are split across up to `max_batches` requests and
    # merged, rather than being truncated away.
    max_diff_chars: int = 200_000
    max_file_diff_chars: int = 40_000
    max_batches: int = 8
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

        timeout = int(_env("VLLM_TIMEOUT", "600"))
        if timeout < MIN_TIMEOUT_SECONDS:
            print(
                f"[config] VLLM_TIMEOUT={timeout}s is below the {MIN_TIMEOUT_SECONDS}s minimum; "
                f"using {MIN_TIMEOUT_SECONDS}s."
            )
            timeout = MIN_TIMEOUT_SECONDS

        llm = LLMSettings(
            base_url=(_env("VLLM_BASE_URL", required=True) or "").rstrip("/"),
            model=_env("VLLM_MODEL", "Qwen/Qwen3.6-35B-A3B-FP8"),
            api_key=_env("VLLM_API_KEY", "not-needed"),
            timeout=timeout,
            max_tokens=int(_env("VLLM_MAX_TOKENS", "32768")),
            temperature=float(_env("VLLM_TEMPERATURE", "0.6")),
            top_p=float(_env("VLLM_TOP_P", "0.95")),
            top_k=int(_env("VLLM_TOP_K", "20")),
            presence_penalty=float(_env("VLLM_PRESENCE_PENALTY", "0.0")),
            enable_thinking=_env_bool("VLLM_ENABLE_THINKING", "true"),
        )

        return cls(
            gitlab_url=gitlab_url.rstrip("/"),
            gitlab_token=gitlab_token,
            project_id=project_id,
            mr_iid=mr_iid,
            llm=llm,
            describe_language=_env("DESCRIBE_LANGUAGE", "Korean"),
            max_diff_chars=int(_env("MAX_DIFF_CHARS", "200000")),
            max_file_diff_chars=int(_env("MAX_FILE_DIFF_CHARS", "40000")),
            max_batches=int(_env("MAX_BATCHES", "8")),
            max_comments=int(_env("MAX_COMMENTS", "25")),
            post_inline_comments=_env_bool("POST_INLINE_COMMENTS", "true"),
            post_summary_comment=_env_bool("POST_SUMMARY_COMMENT", "true"),
            config_path=Path(_env("REVIEW_BOT_CONFIG_PATH", ".gitlab/review-bot.yml")),
            project_dir=Path(_env("CI_PROJECT_DIR", ".")),
        )
