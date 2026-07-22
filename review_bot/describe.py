"""Generates a merge request title and description from a diff via the internal vLLM service."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from openai import OpenAI

from .prompts import DESCRIBE_FILE_BLOCK, DESCRIBE_SYSTEM_PROMPT, DESCRIBE_USER_HEADER
from .reviewer import _parse_json_response


@dataclass
class MRDescription:
    title: str
    description: str


class MRDescriber:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "not-needed",
        timeout: int = 120,
        max_tokens: int = 2048,
        temperature: float = 0.2,
        language: str = "Korean",
    ):
        self._client = OpenAI(base_url=base_url, api_key=api_key or "not-needed", timeout=timeout)
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._language = language

    def describe(
        self,
        source_branch: str,
        target_branch: str,
        commit_subjects: List[str],
        file_blocks: List[Dict[str, Any]],
    ) -> MRDescription:
        """file_blocks: list of {"path", "diff"}."""
        prompt = DESCRIBE_USER_HEADER.format(
            source_branch=source_branch,
            target_branch=target_branch,
            commits="\n".join(f"- {s}" for s in commit_subjects) or "(none)",
        )
        for block in file_blocks:
            prompt += DESCRIBE_FILE_BLOCK.format(path=block["path"], diff=block["diff"])

        response = self._client.chat.completions.create(
            model=self._model,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            messages=[
                {"role": "system", "content": DESCRIBE_SYSTEM_PROMPT.format(language=self._language)},
                {"role": "user", "content": prompt},
            ],
        )
        text = response.choices[0].message.content if response.choices else ""
        data = _parse_json_response(text or "")
        return MRDescription(
            title=str(data.get("title", "")).strip(),
            description=str(data.get("description", "")).strip(),
        )
