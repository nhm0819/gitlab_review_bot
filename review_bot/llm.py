"""Shared transport for the internal vLLM (OpenAI-compatible) service.

Centralizes three things both the reviewer and the describer need:
  - the Qwen3.6 sampling parameters, including the ones that only travel
    through `extra_body` (top_k, chat_template_kwargs)
  - stripping `<think>...</think>` reasoning blocks, since Qwen3.6 runs in
    thinking mode by default and would otherwise corrupt JSON parsing
  - tolerant JSON parsing of the final answer
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict

from openai import OpenAI

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


@dataclass
class LLMSettings:
    """Defaults tuned for Qwen/Qwen3.6-35B-A3B-FP8 served by vLLM.

    Sampling values follow the model card's "thinking mode for precise coding
    tasks" preset, which is the closest match to structured code review.
    """

    base_url: str
    model: str = "Qwen/Qwen3.6-35B-A3B-FP8"
    api_key: str = "not-needed"
    timeout: int = 600
    max_tokens: int = 32768
    temperature: float = 0.6
    top_p: float = 0.95
    top_k: int = 20
    presence_penalty: float = 0.0
    enable_thinking: bool = True


def build_client(settings: LLMSettings) -> OpenAI:
    return OpenAI(
        base_url=settings.base_url,
        api_key=settings.api_key or "not-needed",
        timeout=settings.timeout,
    )


def complete_json(
    client: OpenAI,
    settings: LLMSettings,
    system_prompt: str,
    user_prompt: str,
) -> Dict[str, Any]:
    """Send one chat completion and parse the final answer as JSON."""
    extra_body: Dict[str, Any] = {"top_k": settings.top_k}
    if not settings.enable_thinking:
        # Qwen3.6 thinks by default; this is the documented way to turn it off.
        extra_body["chat_template_kwargs"] = {"enable_thinking": False}

    response = client.chat.completions.create(
        model=settings.model,
        max_tokens=settings.max_tokens,
        temperature=settings.temperature,
        top_p=settings.top_p,
        presence_penalty=settings.presence_penalty,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        extra_body=extra_body,
    )
    return _parse_json_response(strip_thinking(_message_text(response)))


def _message_text(response: Any) -> str:
    if not getattr(response, "choices", None):
        return ""
    return response.choices[0].message.content or ""


def strip_thinking(text: str) -> str:
    """Drop the reasoning block Qwen3.6 emits before its final answer.

    When vLLM runs with `--reasoning-parser qwen3` the reasoning is already
    split into `reasoning_content` and this is a no-op. Without it, the block
    arrives inline and has to be removed before JSON parsing.
    """
    if "</think>" in text:
        return text.rsplit("</think>", 1)[1].strip()
    if "<think>" in text:
        raise ValueError(
            "model output ended while still inside a <think> block -- "
            "raise VLLM_MAX_TOKENS or set VLLM_ENABLE_THINKING=false"
        )
    return text.strip()


def _parse_json_response(text: str) -> Dict[str, Any]:
    cleaned = _FENCE_RE.sub("", text).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Fall back to extracting the first {...} block in case of stray prose.
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))
