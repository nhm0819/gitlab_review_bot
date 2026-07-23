"""Configuration defaults, the timeout floor, and the protected-variable hint."""
import pytest

from review_bot.config import MIN_TIMEOUT_SECONDS, Config, ConfigError

BASE = {
    "CI_SERVER_URL": "https://gitlab.example.com",
    "CI_PROJECT_ID": "1",
    "CI_MERGE_REQUEST_IID": "2",
    "GITLAB_TOKEN": "glpat-x",
    "VLLM_BASE_URL": "http://vllm.internal:8000/v1/",
}


def load(monkeypatch, **extra):
    for key, value in {**BASE, **extra}.items():
        monkeypatch.setenv(key, value)
    return Config.from_env()


def test_qwen_defaults(monkeypatch):
    cfg = load(monkeypatch)
    assert cfg.llm.model == "Qwen/Qwen3.6-35B-A3B-FP8"
    assert (cfg.llm.temperature, cfg.llm.top_p, cfg.llm.top_k) == (0.6, 0.95, 20)
    assert cfg.llm.max_tokens == 32768
    assert cfg.llm.enable_thinking is True
    assert cfg.llm.max_retries == 3
    assert cfg.describe_language == "Korean"


def test_base_url_trailing_slash_is_stripped(monkeypatch):
    assert load(monkeypatch).llm.base_url == "http://vllm.internal:8000/v1"


def test_timeout_floor_is_enforced(monkeypatch):
    assert load(monkeypatch, VLLM_TIMEOUT="60").llm.timeout == MIN_TIMEOUT_SECONDS


def test_timeout_above_floor_is_honoured(monkeypatch):
    assert load(monkeypatch, VLLM_TIMEOUT="900").llm.timeout == 900


def test_thinking_can_be_disabled(monkeypatch):
    assert load(monkeypatch, VLLM_ENABLE_THINKING="false").llm.enable_thinking is False


def test_missing_required_variable_raises(monkeypatch):
    for key, value in BASE.items():
        if key != "VLLM_BASE_URL":
            monkeypatch.setenv(key, value)
    with pytest.raises(ConfigError, match="VLLM_BASE_URL"):
        Config.from_env()


def test_missing_token_in_ci_explains_protected_variables(monkeypatch):
    """The protected-variable checkbox is the usual cause and is easy to miss."""
    for key, value in BASE.items():
        if key != "GITLAB_TOKEN":
            monkeypatch.setenv(key, value)
    monkeypatch.setenv("CI_JOB_ID", "123")
    with pytest.raises(ConfigError, match="Protect variable"):
        Config.from_env()


def test_missing_token_outside_ci_stays_terse(monkeypatch):
    for key, value in BASE.items():
        if key != "GITLAB_TOKEN":
            monkeypatch.setenv(key, value)
    monkeypatch.delenv("CI_JOB_ID", raising=False)
    with pytest.raises(ConfigError) as exc:
        Config.from_env()
    assert "Protect variable" not in str(exc.value)
