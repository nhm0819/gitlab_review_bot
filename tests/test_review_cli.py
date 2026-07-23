"""End-to-end: a diff too large for one request must still be reviewed in full."""
import importlib

import pytest

from conftest import RecordingHandler, chat_completion, json_response, read_json, serve

NUM_FILES = 12
FILE_LINES = 130


def make_diff(index):
    body = "".join(f"+    line_{index}_{j} = compute({j})\n" for j in range(FILE_LINES))
    return f"@@ -1,2 +1,{FILE_LINES} @@\n def f{index}():\n{body}"


@pytest.fixture
def gitlab():
    state = {"posted": [], "mr": {}}

    class Handler(RecordingHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path.endswith("/diffs"):
                json_response(self, [
                    {"new_path": f"src/mod{i}.py", "old_path": f"src/mod{i}.py", "diff": make_diff(i)}
                    for i in range(NUM_FILES)
                ])
            elif path.endswith("/notes"):
                json_response(self, [])
            elif path.endswith("/commits"):
                json_response(self, [{"title": "c"}])
            else:
                json_response(self, state["mr"])

        def do_POST(self):
            state["posted"].append(read_json(self))
            json_response(self, {"id": 1})

    url, _ = serve(Handler)
    state["url"] = url
    state["mr"] = {
        "title": "Big refactor", "description": "", "labels": [],
        "source_branch": "feature/big", "target_branch": "main",
        "author": {"username": "alice"}, "changes_count": str(NUM_FILES),
        "diff_refs": {"base_sha": "b", "start_sha": "s", "head_sha": "h"},
    }
    return state


@pytest.fixture
def vllm():
    state = {"calls": [], "files_seen": set()}

    class Handler(RecordingHandler):
        def do_POST(self):
            body = read_json(self)
            user = body["messages"][1]["content"]
            for i in range(NUM_FILES):
                if f"src/mod{i}.py" in user:
                    state["files_seen"].add(f"src/mod{i}.py")
            if "merging several partial code review summaries" in body["messages"][0]["content"]:
                state["calls"].append("reduce")
                answer = {"summary": "merged overall summary"}
            else:
                state["calls"].append("map")
                answer = {"summary": "partial", "comments": [
                    {"file": "src/mod0.py", "line": 2, "severity": "bug", "comment": "check"}]}
            json_response(self, chat_completion(body["model"], answer))

    url, _ = serve(Handler)
    state["url"] = url
    return state


def run(monkeypatch, gitlab, vllm, **overrides):
    env = {
        "GITLAB_URL": gitlab["url"], "GITLAB_TOKEN": "t",
        "CI_PROJECT_ID": "1", "CI_MERGE_REQUEST_IID": "2",
        "VLLM_BASE_URL": vllm["url"] + "/v1", "VLLM_MODEL": "test-model",
        "CI_PROJECT_DIR": "/nonexistent", "LOG_LEVEL": "CRITICAL",
        "MAX_DIFF_CHARS": "12000", "MAX_BATCHES": "8",
    }
    env.update(overrides)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    from review_bot import cli
    importlib.reload(cli)
    assert cli.main() == 0


def test_large_diff_is_split_across_several_passes(monkeypatch, gitlab, vllm):
    run(monkeypatch, gitlab, vllm)
    assert vllm["calls"].count("map") > 1


def test_every_file_reaches_the_model(monkeypatch, gitlab, vllm):
    run(monkeypatch, gitlab, vllm)
    assert vllm["files_seen"] == {f"src/mod{i}.py" for i in range(NUM_FILES)}, \
        "a file must never be silently skipped"


def test_partial_summaries_are_merged_exactly_once(monkeypatch, gitlab, vllm):
    run(monkeypatch, gitlab, vllm)
    assert vllm["calls"].count("reduce") == 1
    summaries = [p for p in gitlab["posted"] if "AI Code Review" in p.get("body", "")]
    assert summaries and "merged overall summary" in summaries[0]["body"]


def test_small_diff_uses_a_single_pass(monkeypatch, gitlab, vllm):
    run(monkeypatch, gitlab, vllm, MAX_DIFF_CHARS="500000")
    assert vllm["calls"].count("map") == 1
    assert vllm["calls"].count("reduce") == 0, "merging one summary needs no extra call"


def test_draft_merge_request_is_skipped(monkeypatch, gitlab, vllm):
    gitlab["mr"]["draft"] = True
    run(monkeypatch, gitlab, vllm)
    assert vllm["calls"] == []
    assert gitlab["posted"] == []


def test_upstream_truncation_is_reported_in_the_summary(monkeypatch, gitlab, vllm):
    """If GitLab capped the diff, the reader must know the review was partial."""
    gitlab["mr"]["changes_count"] = "1000+"
    run(monkeypatch, gitlab, vllm, MAX_DIFF_CHARS="500000")
    summaries = [p for p in gitlab["posted"] if "AI Code Review" in p.get("body", "")]
    assert "too large to return in full" in summaries[0]["body"]
