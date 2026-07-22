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

import sys
from typing import Dict

from .config import Config, ConfigError
from .diff_parser import addable_lines, added_line_numbers
from .exclude_rules import ReviewRules
from .gitlab_client import GitLabClient
from .reviewer import VLLMReviewer

MARKER_PREFIX = "<!-- ai-review-bot:head_sha="


def main() -> int:
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        print(f"[review-bot] configuration error: {exc}", file=sys.stderr)
        return 1

    client = GitLabClient(cfg.gitlab_url, cfg.gitlab_token, cfg.project_id, cfg.mr_iid)
    mr = client.get_mr()

    if mr.get("draft") or mr.get("work_in_progress"):
        print("[review-bot] merge request is a draft, skipping review.")
        return 0

    head_sha = mr["diff_refs"]["head_sha"]
    if _already_reviewed(client, head_sha):
        print(f"[review-bot] head commit {head_sha} was already reviewed, skipping.")
        return 0

    rules = ReviewRules.load(cfg.project_dir / cfg.config_path)
    author_username = (mr.get("author") or {}).get("username", "")
    if rules.skip_mr(mr["target_branch"], mr["source_branch"], author_username):
        print("[review-bot] merge request matches an exclude rule, skipping.")
        return 0

    changes = client.get_mr_changes()
    file_blocks = []
    line_lookup: Dict[str, Dict[int, str]] = {}
    total_chars = 0

    for change in changes:
        new_path = change.get("new_path") or change.get("old_path")
        if not new_path or change.get("deleted_file"):
            continue
        if rules.skip_path(new_path):
            continue
        diff_text = change.get("diff", "")
        if not diff_text:
            continue
        added = added_line_numbers(diff_text)
        if not added:
            continue
        if total_chars + len(diff_text) > cfg.max_diff_chars:
            print(f"[review-bot] diff budget exhausted, skipping remaining files starting at {new_path}")
            break
        total_chars += len(diff_text)

        file_blocks.append({"path": new_path, "diff": diff_text, "commentable_lines": added})
        line_lookup[new_path] = addable_lines(diff_text)

    if not file_blocks:
        print("[review-bot] no reviewable file changes found, skipping.")
        return 0

    reviewer = VLLMReviewer(cfg.llm)
    result = reviewer.review(
        title=mr.get("title", ""),
        description=mr.get("description", ""),
        custom_instructions=rules.custom_instructions,
        file_blocks=file_blocks,
    )

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
        client.post_note("\n".join(summary_lines))

    print(
        f"[review-bot] review complete: {posted_inline} inline comment(s), "
        f"summary posted={cfg.post_summary_comment}"
    )
    return 0


def _already_reviewed(client: GitLabClient, head_sha: str) -> bool:
    marker = f"{MARKER_PREFIX}{head_sha} -->"
    try:
        notes = client.get_notes()
    except Exception:
        return False
    return any(marker in (note.get("body") or "") for note in notes)


if __name__ == "__main__":
    sys.exit(main())
