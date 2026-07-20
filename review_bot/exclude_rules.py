"""Load and evaluate per-project review exclude rules.

Projects can commit a `.gitlab/review-bot.yml` file (see
examples/review-bot.example.yml) to skip certain branches, authors, or file
paths, and to add custom review instructions -- similar in spirit to
GitLab Duo's own exclusion-rule file.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

import yaml


@dataclass
class ReviewRules:
    exclude_target_branches: List[str] = field(default_factory=list)
    exclude_source_branches: List[str] = field(default_factory=list)
    exclude_authors: List[str] = field(default_factory=list)
    exclude_paths: List[str] = field(default_factory=list)
    custom_instructions: str = ""

    @classmethod
    def load(cls, path: Path) -> "ReviewRules":
        if not path.exists():
            return cls()
        data: Dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        exclude = data.get("exclude", {}) or {}
        paths = data.get("paths", {}) or {}
        return cls(
            exclude_target_branches=exclude.get("target_branches", []) or [],
            exclude_source_branches=exclude.get("source_branches", []) or [],
            exclude_authors=exclude.get("authors", []) or [],
            exclude_paths=paths.get("exclude", []) or [],
            custom_instructions=(data.get("instructions") or "").strip(),
        )

    def skip_mr(self, target_branch: str, source_branch: str, author_username: str) -> bool:
        return (
            _matches_any(target_branch, self.exclude_target_branches)
            or _matches_any(source_branch, self.exclude_source_branches)
            or _matches_any(author_username, self.exclude_authors)
        )

    def skip_path(self, path: str) -> bool:
        return _matches_any(path, self.exclude_paths)


def _matches_any(value: str, patterns: List[str]) -> bool:
    return any(fnmatch.fnmatch(value, pattern) for pattern in patterns)
