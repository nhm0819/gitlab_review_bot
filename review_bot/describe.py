"""Generates a merge request title and description via the internal vLLM service."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from .llm import LLMSettings, build_client, complete_json
from .prompts import (
    DESCRIBE_BATCH_SYSTEM_PROMPT,
    DESCRIBE_FILE_BLOCK,
    DESCRIBE_NOTES_BLOCK,
    DESCRIBE_REDUCE_SYSTEM_PROMPT,
    DESCRIBE_SYSTEM_PROMPT,
    DESCRIBE_USER_HEADER,
)


@dataclass
class MRDescription:
    title: str
    description: str


class MRDescriber:
    def __init__(self, settings: LLMSettings, language: str = "Korean"):
        self._settings = settings
        self._client = build_client(settings)
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

        data = complete_json(
            self._client,
            self._settings,
            DESCRIBE_SYSTEM_PROMPT.format(language=self._language),
            prompt,
        )
        return MRDescription(
            title=str(data.get("title", "")).strip(),
            description=str(data.get("description", "")).strip(),
        )

    def summarize_batch(
        self,
        source_branch: str,
        target_branch: str,
        commit_subjects: List[str],
        file_blocks: List[Dict[str, Any]],
    ) -> str:
        """Map step: terse notes for one subset of the changed files."""
        prompt = DESCRIBE_USER_HEADER.format(
            source_branch=source_branch,
            target_branch=target_branch,
            commits="\n".join(f"- {s}" for s in commit_subjects) or "(none)",
        )
        for block in file_blocks:
            prompt += DESCRIBE_FILE_BLOCK.format(path=block["path"], diff=block["diff"])
        data = complete_json(self._client, self._settings, DESCRIBE_BATCH_SYSTEM_PROMPT, prompt)
        return str(data.get("notes", "")).strip()

    def synthesize(
        self,
        source_branch: str,
        target_branch: str,
        commit_subjects: List[str],
        notes: List[str],
    ) -> MRDescription:
        """Reduce step: build the title and description from per-batch notes."""
        prompt = DESCRIBE_USER_HEADER.format(
            source_branch=source_branch,
            target_branch=target_branch,
            commits="\n".join(f"- {s}" for s in commit_subjects) or "(none)",
        )
        for index, note in enumerate([n for n in notes if n.strip()], 1):
            prompt += DESCRIBE_NOTES_BLOCK.format(index=index, notes=note)
        data = complete_json(
            self._client,
            self._settings,
            DESCRIBE_REDUCE_SYSTEM_PROMPT.format(language=self._language),
            prompt,
        )
        return MRDescription(
            title=str(data.get("title", "")).strip(),
            description=str(data.get("description", "")).strip(),
        )
