"""Calls an internal vLLM service (OpenAI-compatible API) to generate a review.

vLLM exposes an OpenAI-compatible `/v1/chat/completions` endpoint, so this
uses the `openai` SDK pointed at the internal vLLM base URL. No external API
calls are made -- everything stays within the internal network.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from openai import OpenAI

from .prompts import FILE_BLOCK, SYSTEM_PROMPT, USER_PROMPT_HEADER

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


@dataclass
class ReviewComment:
    file: str
    line: int
    severity: str
    comment: str


@dataclass
class ReviewResult:
    summary: str
    comments: List[ReviewComment]


class VLLMReviewer:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "not-needed",
        timeout: int = 120,
        max_tokens: int = 4096,
        temperature: float = 0.2,
    ):
        # api_key is usually irrelevant for an internal vLLM server, but the
        # OpenAI client requires a non-empty value, so a placeholder is used.
        self._client = OpenAI(base_url=base_url, api_key=api_key or "not-needed", timeout=timeout)
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature

    def review(
        self,
        title: str,
        description: str,
        custom_instructions: str,
        file_blocks: List[Dict[str, Any]],
    ) -> ReviewResult:
        """file_blocks: list of {"path", "diff", "commentable_lines": [int, ...]}."""
        prompt = USER_PROMPT_HEADER.format(
            title=title or "(no title)",
            description=description or "(no description)",
            custom_instructions=(
                f"Additional project review instructions:\n{custom_instructions}\n\n" if custom_instructions else ""
            ),
        )
        for block in file_blocks:
            prompt += FILE_BLOCK.format(
                path=block["path"],
                commentable_lines=block["commentable_lines"],
                diff=block["diff"],
            )

        response = self._client.chat.completions.create(
            model=self._model,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        text = _extract_text(response)
        data = _parse_json_response(text)

        comments = [
            ReviewComment(
                file=str(c.get("file", "")),
                line=int(c.get("line", 0)),
                severity=str(c.get("severity", "suggestion")),
                comment=str(c.get("comment", "")).strip(),
            )
            for c in data.get("comments", [])
            if c.get("file") and c.get("line") and c.get("comment")
        ]
        return ReviewResult(summary=str(data.get("summary", "")).strip(), comments=comments)


def _extract_text(response: Any) -> str:
    if not getattr(response, "choices", None):
        return ""
    content = response.choices[0].message.content
    return content or ""


def _parse_json_response(text: str) -> Dict[str, Any]:
    cleaned = _FENCE_RE.sub("", text).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Fall back to extracting the first {...} block in case the model added stray prose.
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))
