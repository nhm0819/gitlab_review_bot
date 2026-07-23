"""Thin wrapper around the GitLab REST API endpoints the bot needs."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import requests

from .retry import request_with_retry

log = logging.getLogger("review_bot.gitlab")

# `/diffs` rejected per_page above 30 on older GitLab versions, so stay at the
# limit that every supported version accepts.
DIFFS_PER_PAGE = 30
MAX_DIFF_PAGES = 100


class GitLabClient:
    def __init__(self, base_url: str, token: str, project_id: str, mr_iid: str, timeout: int = 30):
        self._base = base_url.rstrip("/")
        self._project = project_id
        self._mr_iid = mr_iid
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"PRIVATE-TOKEN": token})

    def _url(self, path: str) -> str:
        return f"{self._base}/api/v4/projects/{self._project}{path}"

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> requests.Response:
        return request_with_retry(
            self._session, "GET", self._url(path),
            idempotent=True, params=params, timeout=self._timeout,
        )

    def get_mr(self) -> Dict[str, Any]:
        resp = self._get(f"/merge_requests/{self._mr_iid}")
        resp.raise_for_status()
        return resp.json()

    def get_mr_file_changes(self, mr: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], bool]:
        """Return (changes, was_truncated_by_gitlab) for this merge request.

        GitLab caps database-backed diffs by size and file count. When a merge
        request exceeds those caps the API quietly returns a partial diff, which
        would make the bot review a subset while believing it saw everything.
        `changes_count` is reported as e.g. "1000+" in that case, and the raw
        diffs (served from Gitaly rather than the database) are not capped.
        """
        overflowed = str(mr.get("changes_count") or "").endswith("+")
        if overflowed:
            log.warning(
                "GitLab reports a truncated diff (changes_count=%s); refetching raw diffs",
                mr.get("changes_count"),
                extra={"fields": {"changes_count": mr.get("changes_count")}},
            )
            changes = self._get_raw_changes()
            if changes:
                return changes, True
            log.warning("raw diff fetch returned nothing; falling back to paginated diffs")

        return self._get_paginated_diffs(), overflowed

    def _get_paginated_diffs(self) -> List[Dict[str, Any]]:
        """List diffs via the endpoint that replaced the deprecated /changes."""
        collected: List[Dict[str, Any]] = []
        for page in range(1, MAX_DIFF_PAGES + 1):
            resp = self._get(
                f"/merge_requests/{self._mr_iid}/diffs",
                params={"page": page, "per_page": DIFFS_PER_PAGE},
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            collected.extend(batch)
            if len(batch) < DIFFS_PER_PAGE:
                break
        else:
            log.warning("stopped paginating diffs at %d pages", MAX_DIFF_PAGES)
        return collected

    def _get_raw_changes(self) -> List[Dict[str, Any]]:
        """Uncapped diffs, read from Gitaly instead of the database.

        Slower and more resource intensive, so this is only used when the
        capped endpoint reports that it truncated the result.
        """
        resp = self._get(
            f"/merge_requests/{self._mr_iid}/changes",
            params={"access_raw_diffs": "true"},
        )
        if resp.status_code >= 400:
            log.warning("raw diff fetch failed with HTTP %d", resp.status_code)
            return []
        return resp.json().get("changes", [])

    def get_mr_commits(self) -> List[Dict[str, Any]]:
        resp = self._get(f"/merge_requests/{self._mr_iid}/commits", params={"per_page": 100})
        resp.raise_for_status()
        return resp.json()

    def get_notes(self) -> List[Dict[str, Any]]:
        resp = self._get(
            f"/merge_requests/{self._mr_iid}/notes",
            params={"per_page": 100, "order_by": "created_at", "sort": "desc"},
        )
        resp.raise_for_status()
        return resp.json()

    def update_mr(self, title: Optional[str] = None, description: Optional[str] = None) -> Dict[str, Any]:
        """Update the MR title and/or description. Fields left as None are untouched."""
        payload: Dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if description is not None:
            payload["description"] = description
        if not payload:
            return {}
        # Setting absolute values, so replaying this cannot compound.
        resp = request_with_retry(
            self._session, "PUT", self._url(f"/merge_requests/{self._mr_iid}"),
            idempotent=True, json=payload, timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def post_note(self, body: str) -> Dict[str, Any]:
        resp = request_with_retry(
            self._session, "POST", self._url(f"/merge_requests/{self._mr_iid}/notes"),
            idempotent=False, json={"body": body}, timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def post_inline_discussion(
        self,
        body: str,
        diff_refs: Dict[str, str],
        old_path: str,
        new_path: str,
        new_line: int,
    ) -> Optional[Dict[str, Any]]:
        payload = {
            "body": body,
            "position": {
                "base_sha": diff_refs["base_sha"],
                "start_sha": diff_refs["start_sha"],
                "head_sha": diff_refs["head_sha"],
                "position_type": "text",
                "old_path": old_path,
                "new_path": new_path,
                "new_line": new_line,
            },
        }
        resp = request_with_retry(
            self._session, "POST", self._url(f"/merge_requests/{self._mr_iid}/discussions"),
            idempotent=False, json=payload, timeout=self._timeout,
        )
        # A given line can occasionally be rejected (e.g. outdated position);
        # degrade gracefully instead of failing the whole review.
        if resp.status_code >= 400:
            return None
        return resp.json()
