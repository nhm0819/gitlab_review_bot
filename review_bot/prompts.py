"""Prompt templates used to request a structured code review from the LLM."""

SYSTEM_PROMPT = """You are a senior software engineer performing a merge request code review.
Review only the diff you are given. Focus on, in priority order:
1. Correctness bugs and logic errors
2. Security vulnerabilities (injection, auth/access control, secrets, unsafe deserialization, SSRF, etc.)
3. Reliability and error-handling gaps
4. Clear, high-value maintainability issues (skip nitpicks that don't matter)

Rules:
- Only comment on lines that appear in the "Commentable new-file line numbers" list for that file.
- Do not invent line numbers.
- Be concise: one to three sentences per comment.
- If the same issue repeats across many files, mention it once in the summary instead of once per file.
- If the diff looks fine, say so plainly instead of inventing issues.
- Respond with ONLY a single JSON object. No markdown code fences, no prose outside the JSON.

JSON schema:
{
  "summary": "2-5 sentence overview of the change and overall review verdict",
  "comments": [
    {"file": "path/as/given", "line": 123, "severity": "bug|security|reliability|style|suggestion", "comment": "..."}
  ]
}
"""

USER_PROMPT_HEADER = """Merge request title: {title}

Merge request description:
{description}

{custom_instructions}
Below are the changed files. Each file lists its diff, followed by the exact
new-file line numbers you are allowed to attach a comment to.
"""

FILE_BLOCK = """
--- FILE: {path} ---
Commentable new-file line numbers: {commentable_lines}

Diff:
```diff
{diff}
```
"""

DESCRIBE_SYSTEM_PROMPT = """You write a merge request title and description from a code diff.

Rules:
- Title: imperative mood, at most 72 characters, no trailing period, no "Draft:" prefix,
  and no issue/ticket IDs unless they appear in the branch name or commit subjects.
- Description: concise markdown using these sections, with the headings written in {language}:
  "Summary"             - 1-3 sentences on what changed and why
  "Key changes"         - bullet list, grouped by concern rather than by file
  "Notes for reviewers" - anything needing attention; omit this section entirely if nothing notable
- Describe only what the diff actually shows. Never invent motivation, ticket numbers,
  benchmarks, or testing that is not evident from the changes.
- Do not list every file. Summarize.
- Write the title and description in {language}.
- Respond with ONLY a single JSON object. No markdown code fences, no prose outside the JSON.

JSON schema:
{{"title": "...", "description": "..."}}
"""

DESCRIBE_USER_HEADER = """Source branch: {source_branch}
Target branch: {target_branch}

Commit subjects on this merge request:
{commits}

Below are the changed files and their diffs.
"""

DESCRIBE_FILE_BLOCK = """
--- FILE: {path} ---
```diff
{diff}
```
"""
