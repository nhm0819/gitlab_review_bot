"""Calls the Anthropic API to generate a structured code review."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List

import anthropic

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


class ClaudeReviewer:
    def __init__(self, api_key: str, model: str):
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

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

        response = self._client.messages.create(
            model=self._model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(part.text for part in response.content if getattr(part, "type", None) == "text")
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
