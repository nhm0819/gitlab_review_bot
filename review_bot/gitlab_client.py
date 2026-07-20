"""Thin wrapper around the GitLab REST API endpoints the bot needs."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests


class GitLabClient:
    def __init__(self, base_url: str, token: str, project_id: str, mr_iid: str, timeout: int = 30):
        self._base = base_url.rstrip("/")
        self._headers = {"PRIVATE-TOKEN": token}
        self._project = project_id
        self._mr_iid = mr_iid
        self._timeout = timeout

    def _url(self, path: str) -> str:
        return f"{self._base}/api/v4/projects/{self._project}{path}"

    def get_mr(self) -> Dict[str, Any]:
        resp = requests.get(self._url(f"/merge_requests/{self._mr_iid}"), headers=self._headers, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def get_mr_changes(self) -> List[Dict[str, Any]]:
        resp = requests.get(
            self._url(f"/merge_requests/{self._mr_iid}/changes"), headers=self._headers, timeout=self._timeout
        )
        resp.raise_for_status()
        return resp.json().get("changes", [])

    def get_notes(self) -> List[Dict[str, Any]]:
        resp = requests.get(
            self._url(f"/merge_requests/{self._mr_iid}/notes"),
            headers=self._headers,
            params={"per_page": 100, "order_by": "created_at", "sort": "desc"},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def post_note(self, body: str) -> Dict[str, Any]:
        resp = requests.post(
            self._url(f"/merge_requests/{self._mr_iid}/notes"),
            headers=self._headers,
            json={"body": body},
            timeout=self._timeout,
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
        resp = requests.post(
            self._url(f"/merge_requests/{self._mr_iid}/discussions"),
            headers=self._headers,
            json=payload,
            timeout=self._timeout,
        )
        # A given line can occasionally be rejected (e.g. outdated position);
        # degrade gracefully instead of failing the whole review.
        if resp.status_code >= 400:
            return None
        return resp.json()
