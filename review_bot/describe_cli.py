"""Entry point: generate a merge request title and description from its diff.

Policy:
  Default            - fill the title only if it still looks auto-generated (GitLab
                       derives it from the branch name or a single commit subject),
                       and fill the description only if it is empty. Anything a human
                       wrote is left alone.
  Opt-in             - if the MR carries the `ai:describe` label or its description
                       contains `/ai-describe`, the generated summary is appended to
                       the existing description inside marker comments instead of
                       replacing it. A human-written title is still preserved.

Re-running is idempotent: an existing marker block is replaced rather than stacked.

Run as a GitLab CI/CD job (see .gitlab-ci.yml) or locally for testing:

    export GITLAB_URL=https://gitlab.example.com
    export GITLAB_TOKEN=glpat-...
    export CI_PROJECT_ID=123
    export CI_MERGE_REQUEST_IID=45
    export VLLM_BASE_URL=http://vllm.internal.svc.cluster.local:8000/v1
    export VLLM_MODEL=Qwen/Qwen3.6-35B-A3B-FP8
    python -m review_bot.describe_cli
"""
from __future__ import annotations

import logging
import re
import sys
import time
from typing import List, Optional

from .batching import FileChange, batch, estimate_tokens, prepare
from .config import Config, ConfigError
from .diff_parser import added_line_numbers
from .describe import MRDescriber
from .exclude_rules import ReviewRules
from .gitlab_client import GitLabClient
from .logging_setup import setup_logging, shutdown_logging

MARKER_START = "<!-- ai-describe:start -->"
MARKER_END = "<!-- ai-describe:end -->"
DESCRIBE_LABEL = "ai:describe"
DESCRIBE_COMMAND = "/ai-describe"

_DRAFT_RE = re.compile(r"^\s*(draft:|wip:)\s*", re.IGNORECASE)
_NORMALIZE_RE = re.compile(r"[^0-9a-z\uac00-\ud7a3]+")
_LEADING_ID_RE = re.compile(r"^\d+[-_]")


def _run(log: logging.Logger) -> int:
    started = time.monotonic()
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return 1

    client = GitLabClient(cfg.gitlab_url, cfg.gitlab_token, cfg.project_id, cfg.mr_iid)
    mr = client.get_mr()

    raw_title = mr.get("title") or ""
    draft_match = _DRAFT_RE.match(raw_title)
    draft_prefix = draft_match.group(0) if draft_match else ""
    bare_title = raw_title[len(draft_prefix):]

    existing_description = mr.get("description") or ""
    labels = mr.get("labels") or []
    opt_in = DESCRIBE_LABEL in labels or DESCRIBE_COMMAND in existing_description

    commit_subjects = [c.get("title", "") for c in client.get_mr_commits() if c.get("title")]
    title_is_auto = _looks_auto_generated(bare_title, mr.get("source_branch", ""), commit_subjects)
    description_is_empty = not existing_description.strip()

    if not opt_in and not title_is_auto and not description_is_empty:
        log.info("title and description were both written by a human, nothing to do",
                 extra={"fields": {"skipped": "human_authored"}})
        return 0

    rules = ReviewRules.load(cfg.project_dir / cfg.config_path)
    if rules.skip_mr(mr.get("target_branch", ""), mr.get("source_branch", ""), (mr.get("author") or {}).get("username", "")):
        log.info("merge request matches an exclude rule, skipping",
                 extra={"fields": {"skipped": "exclude_rule"}})
        return 0

    collected = _collect_changes(client, rules, mr)
    if not collected:
        log.info("no reviewable file changes found, skipping",
                 extra={"fields": {"skipped": "no_changes"}})
        return 0

    ranked = prepare(collected, cfg.max_file_diff_chars)
    batches, dropped = batch(ranked, cfg.max_diff_chars, cfg.max_batches)
    total_chars = sum(c.size for group in batches for c in group)
    log.info(
        "planned description of %d file(s) in %d batch(es)", len(collected), len(batches),
        extra={"fields": {
            "files": len(collected), "batches": len(batches),
            "estimated_tokens": estimate_tokens(total_chars), "dropped_files": len(dropped),
        }},
    )

    describer = MRDescriber(cfg.llm, language=cfg.describe_language)
    source_branch = mr.get("source_branch", "")
    target_branch = mr.get("target_branch", "")

    if len(batches) == 1:
        generated = describer.describe(
            source_branch=source_branch,
            target_branch=target_branch,
            commit_subjects=commit_subjects,
            file_blocks=[{"path": c.path, "diff": c.diff} for c in batches[0]],
        )
    else:
        # Hierarchical summarization: notes per batch, then one synthesis pass.
        notes = []
        for index, group in enumerate(batches, 1):
            log.info(
                "summarizing batch %d/%d", index, len(batches),
                extra={"fields": {"batch": index, "batches": len(batches), "files": len(group)}},
            )
            notes.append(
                describer.summarize_batch(
                    source_branch=source_branch,
                    target_branch=target_branch,
                    commit_subjects=commit_subjects,
                    file_blocks=[{"path": c.path, "diff": c.diff} for c in group],
                )
            )
        generated = describer.synthesize(
            source_branch=source_branch,
            target_branch=target_branch,
            commit_subjects=commit_subjects,
            notes=notes,
        )

    new_title: Optional[str] = None
    if title_is_auto and generated.title:
        new_title = f"{draft_prefix}{generated.title}"

    new_description: Optional[str] = None
    if generated.description:
        if opt_in:
            new_description = _merge_description(existing_description, generated.description)
        elif description_is_empty:
            new_description = _wrap(generated.description)

    if new_title is None and new_description is None:
        log.info("nothing to update", extra={"fields": {"skipped": "nothing_generated"}})
        return 0

    client.update_mr(title=new_title, description=new_description)
    log.info(
        "merge request updated (mode=%s)", "append" if opt_in else "fill",
        extra={"fields": {
            "mode": "append" if opt_in else "fill",
            "title_updated": bool(new_title),
            "description_updated": bool(new_description),
            "batches": len(batches),
            "duration_seconds": round(time.monotonic() - started, 2),
        }},
    )
    return 0


def main() -> int:
    log = setup_logging("describe")
    try:
        return _run(log)
    except Exception:
        log.exception("description generation failed with an unhandled error")
        return 1
    finally:
        shutdown_logging()


def _collect_changes(client: GitLabClient, rules: ReviewRules, mr: dict) -> List[FileChange]:
    raw_changes, _upstream_truncated = client.get_mr_file_changes(mr)
    changes: List[FileChange] = []
    for change in raw_changes:
        path = change.get("new_path") or change.get("old_path")
        diff = change.get("diff", "")
        if not path or not diff or rules.skip_path(path):
            continue
        changes.append(FileChange(path=path, diff=diff, added_lines=added_line_numbers(diff)))
    return changes


def _normalize(text: str) -> str:
    return _NORMALIZE_RE.sub(" ", text.lower()).strip()


def _branch_to_title(branch: str) -> str:
    name = branch.rsplit("/", 1)[-1]
    name = _LEADING_ID_RE.sub("", name)
    return re.sub(r"[-_]+", " ", name)


def _looks_auto_generated(title: str, source_branch: str, commit_subjects: List[str]) -> bool:
    """True when the title still matches what GitLab auto-fills on MR creation."""
    if not title.strip():
        return True
    normalized = _normalize(title)
    if source_branch and normalized == _normalize(_branch_to_title(source_branch)):
        return True
    if len(commit_subjects) == 1 and normalized == _normalize(commit_subjects[0]):
        return True
    return False


def _wrap(body: str) -> str:
    return f"{MARKER_START}\n{body.strip()}\n{MARKER_END}"


def _merge_description(existing: str, generated: str) -> str:
    """Replace an existing marker block, or append a new one, leaving human text intact."""
    block = _wrap(generated)
    pattern = re.compile(re.escape(MARKER_START) + r".*?" + re.escape(MARKER_END), re.DOTALL)
    if pattern.search(existing):
        return pattern.sub(lambda _: block, existing, count=1)
    if existing.strip():
        return f"{existing.rstrip()}\n\n{block}"
    return block


if __name__ == "__main__":
    sys.exit(main())
