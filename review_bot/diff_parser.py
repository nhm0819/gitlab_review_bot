"""Parse unified diffs to find lines that can carry an inline GitLab comment.

GitLab's Discussions API positions a comment using the *new file* line
number. That line number is only valid if it corresponds to a line that
actually exists in the diff (added or unchanged/context line). This module
turns a single-file unified diff into that lookup table.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

# Prefix used by batching.condense_diff for "N hunks omitted" markers. It is
# skipped here so an annotation can never be mistaken for a context line and
# shift the new-file line numbers that inline comments are anchored to.
OMISSION_PREFIX = "~~~"


@dataclass
class DiffLine:
    kind: str  # "add" | "del" | "context"
    old_line: Optional[int]
    new_line: Optional[int]
    content: str


def parse_diff(diff_text: str) -> List[DiffLine]:
    """Parse a single-file unified diff body into a list of DiffLine records."""
    lines: List[DiffLine] = []
    old_no = new_no = 0
    for raw in diff_text.splitlines():
        hunk = _HUNK_RE.match(raw)
        if hunk:
            old_no = int(hunk.group(1))
            new_no = int(hunk.group(2))
            continue
        if raw.startswith("+++") or raw.startswith("---") or raw.startswith("\\ No newline"):
            continue
        if raw.startswith(OMISSION_PREFIX):
            continue
        if raw.startswith("+"):
            lines.append(DiffLine("add", None, new_no, raw[1:]))
            new_no += 1
        elif raw.startswith("-"):
            lines.append(DiffLine("del", old_no, None, raw[1:]))
            old_no += 1
        else:
            content = raw[1:] if raw.startswith(" ") else raw
            lines.append(DiffLine("context", old_no, new_no, content))
            old_no += 1
            new_no += 1
    return lines


def addable_lines(diff_text: str) -> Dict[int, str]:
    """Return {new_line_number: content} for lines GitLab will accept a comment on.

    Only added and context lines exist in the new file version, so only
    those are valid targets for an inline discussion position.
    """
    result: Dict[int, str] = {}
    for line in parse_diff(diff_text):
        if line.kind in ("add", "context") and line.new_line is not None:
            result[line.new_line] = line.content
    return result


def added_line_numbers(diff_text: str) -> List[int]:
    """Return just the line numbers that were actually added (used for prompting)."""
    return [line.new_line for line in parse_diff(diff_text) if line.kind == "add" and line.new_line is not None]
