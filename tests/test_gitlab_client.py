"""GitLab client: pagination and the upstream diff-truncation fallback."""
import json

from review_bot.gitlab_client import DIFFS_PER_PAGE, GitLabClient

from conftest import RecordingHandler, json_response, serve


def build_server(total_files=5, raw_files=None, diffs_status=200):
    state = {"paths": []}

    class Handler(RecordingHandler):
        def do_GET(self):
            path, _, query = self.path.partition("?")
            state["paths"].append(self.path)
            if path.endswith("/diffs"):
                params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
                page = int(params.get("page", 1))
                per_page = int(params.get("per_page", DIFFS_PER_PAGE))
                start = (page - 1) * per_page
                chunk = [
                    {"new_path": f"src/f{i}.py", "old_path": f"src/f{i}.py", "diff": f"@@ -1 +1 @@\n+f{i}\n"}
                    for i in range(start, min(start + per_page, total_files))
                ]
                json_response(self, chunk, status=diffs_status)
            elif path.endswith("/changes"):
                json_response(self, {"changes": raw_files or []})
            else:
                json_response(self, {})

    url, _ = serve(Handler)
    return GitLabClient(url, "tok", "1", "2"), state


def test_paginates_until_a_short_page():
    client, state = build_server(total_files=DIFFS_PER_PAGE * 2 + 3)
    changes, truncated = client.get_mr_file_changes({"changes_count": "63"})
    assert len(changes) == DIFFS_PER_PAGE * 2 + 3
    assert truncated is False
    assert sum("/diffs" in p for p in state["paths"]) == 3


def test_single_page_makes_one_request():
    client, state = build_server(total_files=4)
    changes, _ = client.get_mr_file_changes({"changes_count": "4"})
    assert len(changes) == 4
    assert sum("/diffs" in p for p in state["paths"]) == 1


def test_overflowed_changes_count_triggers_raw_diff_fetch():
    """GitLab reports "1000+" when it capped the diff; the capped result is incomplete."""
    raw = [{"new_path": "src/big.py", "old_path": "src/big.py", "diff": "@@ -1 +1 @@\n+full\n"}]
    client, state = build_server(total_files=2, raw_files=raw)
    changes, truncated = client.get_mr_file_changes({"changes_count": "1000+"})
    assert truncated is True
    assert changes == raw
    assert any("access_raw_diffs=true" in p for p in state["paths"])


def test_normal_changes_count_never_hits_the_expensive_endpoint():
    client, state = build_server(total_files=2)
    client.get_mr_file_changes({"changes_count": "2"})
    assert not any("/changes" in p for p in state["paths"])


def test_falls_back_to_paginated_diffs_when_raw_fetch_is_empty():
    client, _ = build_server(total_files=2, raw_files=[])
    changes, truncated = client.get_mr_file_changes({"changes_count": "999+"})
    assert truncated is True
    assert len(changes) == 2, "an empty raw response must not leave the review with no diff"


def test_missing_changes_count_is_treated_as_not_truncated():
    client, _ = build_server(total_files=2)
    _, truncated = client.get_mr_file_changes({})
    assert truncated is False


def test_update_mr_sends_only_provided_fields():
    captured = {}

    class Handler(RecordingHandler):
        def do_PUT(self):
            length = int(self.headers.get("Content-Length", 0))
            captured.update(json.loads(self.rfile.read(length)))
            json_response(self, {"ok": True})

    url, _ = serve(Handler)
    GitLabClient(url, "tok", "1", "2").update_mr(description="only this")
    assert captured == {"description": "only this"}


def test_update_mr_with_nothing_makes_no_request():
    class Handler(RecordingHandler):
        called = False

        def do_PUT(self):
            Handler.called = True
            json_response(self, {})

    url, _ = serve(Handler)
    assert GitLabClient(url, "tok", "1", "2").update_mr() == {}
    assert Handler.called is False
