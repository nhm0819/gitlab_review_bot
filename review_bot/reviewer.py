"""Generates a structured code review via the internal vLLM service."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from .llm import LLMSettings, build_client, complete_json
from .prompts import FILE_BLOCK, SYSTEM_PROMPT, USER_PROMPT_HEADER


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
    def __init__(self, settings: LLMSettings):
        self._settings = settings
        self._client = build_client(settings)

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

        data = complete_json(self._client, self._settings, SYSTEM_PROMPT, prompt)

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
