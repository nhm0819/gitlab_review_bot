"""Entry point: fetch an MR diff, review it via the internal vLLM service, post results.

Run as a GitLab CI/CD job (see .gitlab-ci.yml) or locally for testing:

    export GITLAB_URL=https://gitlab.example.com
    export GITLAB_TOKEN=glpat-...
    export CI_PROJECT_ID=123
    export CI_MERGE_REQUEST_IID=45
    export VLLM_BASE_URL=http://vllm.internal.svc.cluster.local:8000/v1
    export VLLM_MODEL=Qwen/Qwen3.6-35B-A3B-FP8
    python -m review_bot.cli
"""
from __future__ import annotations

import logging
import sys
import time
from typing import Dict, List

from .config import Config, ConfigError
from .batching import FileChange, batch, estimate_tokens, prepare
from .diff_parser import addable_lines, added_line_numbers
from .exclude_rules import ReviewRules
from .gitlab_client import GitLabClient
from .logging_setup import setup_logging, shutdown_logging
from .reviewer import ReviewResult, VLLMReviewer

MARKER_PREFIX = "<!-- ai-review-bot:head_sha="


def _run(log: logging.Logger) -> int:
    started = time.monotonic()
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return 1

    client = GitLabClient(cfg.gitlab_url, cfg.gitlab_token, cfg.project_id, cfg.mr_iid)
    mr = client.get_mr()

    if mr.get("draft") or mr.get("work_in_progress"):
        log.info("merge request is a draft, skipping review", extra={"fields": {"skipped": "draft"}})
        return 0

    head_sha = mr["diff_refs"]["head_sha"]
    if _already_reviewed(client, head_sha):
        log.info("head commit already reviewed, skipping", extra={"fields": {"skipped": "duplicate", "head_sha": head_sha}})
        return 0

    rules = ReviewRules.load(cfg.project_dir / cfg.config_path)
    author_username = (mr.get("author") or {}).get("username", "")
    if rules.skip_mr(mr["target_branch"], mr["source_branch"], author_username):
        log.info("merge request matches an exclude rule, skipping", extra={"fields": {"skipped": "exclude_rule"}})
        return 0

    collected: List[FileChange] = []
    for change in client.get_mr_changes():
        new_path = change.get("new_path") or change.get("old_path")
        if not new_path or change.get("deleted_file") or rules.skip_path(new_path):
            continue
        diff_text = change.get("diff", "")
        if not diff_text or not added_line_numbers(diff_text):
            continue
        collected.append(FileChange(path=new_path, diff=diff_text, added_lines=added_line_numbers(diff_text)))

    if not collected:
        log.info("no reviewable file changes found, skipping", extra={"fields": {"skipped": "no_changes"}})
        return 0

    ranked = prepare(collected, cfg.max_file_diff_chars)
    batches, dropped = batch(ranked, cfg.max_diff_chars, cfg.max_batches)
    truncated = [c.path for c in ranked if c.truncated]
    total_chars = sum(c.size for group in batches for c in group)
    log.info(
        "planned review of %d file(s) in %d batch(es)", len(collected), len(batches),
        extra={"fields": {
            "files": len(collected), "batches": len(batches),
            "estimated_tokens": estimate_tokens(total_chars),
            "truncated_files": len(truncated), "dropped_files": len(dropped),
        }},
    )

    reviewer = VLLMReviewer(cfg.llm)
    line_lookup: Dict[str, Dict[int, str]] = {}
    all_comments = []
    summaries = []

    # Map: review each batch independently.
    for index, group in enumerate(batches, 1):
        file_blocks = []
        for change in group:
            # Commentable lines come from the (possibly truncated) diff the
            # model actually saw, so it can never cite a line it was not shown.
            visible = addable_lines(change.diff)
            line_lookup[change.path] = visible
            file_blocks.append({
                "path": change.path,
                "diff": change.diff,
                "commentable_lines": added_line_numbers(change.diff),
            })
        log.info(
            "reviewing batch %d/%d", index, len(batches),
            extra={"fields": {"batch": index, "batches": len(batches), "files": len(group)}},
        )
        partial = reviewer.review(
            title=mr.get("title", ""),
            description=mr.get("description", ""),
            custom_instructions=rules.custom_instructions,
            file_blocks=file_blocks,
        )
        all_comments.extend(partial.comments)
        summaries.append(partial.summary)

    # Reduce: merge the per-batch summaries into one.
    result = ReviewResult(summary=reviewer.reduce_summaries(summaries), comments=all_comments)

    posted_inline = 0
    skipped_comments = []
    if cfg.post_inline_comments:
        for comment in result.comments[: cfg.max_comments]:
            valid_lines = line_lookup.get(comment.file, {})
            if comment.line not in valid_lines:
                skipped_comments.append(comment)
                continue
            body = f"**[{comment.severity}]** {comment.comment}"
            posted = client.post_inline_discussion(
                body=body,
                diff_refs=mr["diff_refs"],
                old_path=comment.file,
                new_path=comment.file,
                new_line=comment.line,
            )
            if posted is None:
                skipped_comments.append(comment)
            else:
                posted_inline += 1

    if cfg.post_summary_comment:
        summary_lines = [
            f"{MARKER_PREFIX}{head_sha} -->",
            "### \U0001f916 AI Code Review",
            "",
            result.summary or "No summary was returned.",
        ]
        if posted_inline:
            summary_lines.append(f"\nPosted {posted_inline} inline comment(s) on this diff.")
        if skipped_comments:
            summary_lines.append("\n**Additional notes:**")
            for c in skipped_comments:
                summary_lines.append(f"- `{c.file}` (line {c.line}, {c.severity}): {c.comment}")
        if truncated or dropped:
            summary_lines.append("\n**Coverage:**")
            if len(batches) > 1:
                summary_lines.append(f"- Reviewed in {len(batches)} passes and merged.")
            for change in ranked:
                if change.truncated:
                    summary_lines.append(
                        f"- `{change.path}` was too large to include in full; "
                        f"the most review-relevant hunks were kept and "
                        f"{change.omitted_hunks} other hunk(s) were omitted."
                    )
            for change in dropped:
                summary_lines.append(f"- `{change.path}` exceeded the review budget and was not reviewed.")
        client.post_note("\n".join(summary_lines))

    log.info(
        "review complete: %d inline comment(s) posted", posted_inline,
        extra={"fields": {
            "inline_comments": posted_inline,
            "skipped_comments": len(skipped_comments),
            "summary_posted": cfg.post_summary_comment,
            "batches": len(batches),
            "duration_seconds": round(time.monotonic() - started, 2),
        }},
    )
    return 0


def main() -> int:
    log = setup_logging("review")
    started_at = time.monotonic()
    try:
        return _run(log)
    except Exception:
        log.exception("review failed with an unhandled error")
        return 1
    finally:
        log.debug("run finished in %.2fs", time.monotonic() - started_at)
        shutdown_logging()


def _already_reviewed(client: GitLabClient, head_sha: str) -> bool:
    marker = f"{MARKER_PREFIX}{head_sha} -->"
    try:
        notes = client.get_notes()
    except Exception:
        return False
    return any(marker in (note.get("body") or "") for note in notes)


if __name__ == "__main__":
    sys.exit(main())
