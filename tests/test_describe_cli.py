"""End-to-end: MR title/description generation and its overwrite policy."""
import importlib

import pytest

from conftest import RecordingHandler, chat_completion, json_response, read_json, serve

GEN_TITLE = "Add retry logic to the payment client"
GEN_DESC = "## 요약\n결제 클라이언트에 재시도 로직을 추가했습니다."

BASE_MR = {
    "source_branch": "feature/add-retry-logic", "target_branch": "main",
    "author": {"username": "alice"}, "labels": [], "diff_refs": {}, "changes_count": "1",
}


@pytest.fixture
def gitlab():
    state = {"mr": {}, "commits": [], "puts": []}

    class Handler(RecordingHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path.endswith("/commits"):
                json_response(self, [{"title": s} for s in state["commits"]])
            elif path.endswith("/diffs"):
                json_response(self, [{"new_path": "pay/client.py", "old_path": "pay/client.py",
                                      "diff": "@@ -1,2 +1,3 @@\n def pay():\n-    return call()\n+    return retry(call)\n"}])
            else:
                json_response(self, state["mr"])

        def do_PUT(self):
            state["puts"].append(read_json(self))
            json_response(self, state["mr"])

    url, _ = serve(Handler)
    state["url"] = url
    return state


@pytest.fixture
def vllm():
    class Handler(RecordingHandler):
        def do_POST(self):
            body = read_json(self)
            json_response(self, chat_completion(
                body["model"], {"title": GEN_TITLE, "description": GEN_DESC}))

    url, _ = serve(Handler)
    return url


def run(monkeypatch, gitlab, vllm, mr, commits=("c1", "c2")):
    gitlab["mr"] = {**BASE_MR, **mr}
    gitlab["commits"] = list(commits)
    gitlab["puts"].clear()
    for key, value in {
        "GITLAB_URL": gitlab["url"], "GITLAB_TOKEN": "t",
        "CI_PROJECT_ID": "1", "CI_MERGE_REQUEST_IID": "2",
        "VLLM_BASE_URL": vllm + "/v1", "VLLM_MODEL": "test-model",
        "CI_PROJECT_DIR": "/nonexistent", "LOG_LEVEL": "CRITICAL",
    }.items():
        monkeypatch.setenv(key, value)

    from review_bot import describe_cli
    importlib.reload(describe_cli)
    assert describe_cli.main() == 0
    return gitlab["puts"]


def test_auto_title_and_empty_description_are_both_filled(monkeypatch, gitlab, vllm):
    puts = run(monkeypatch, gitlab, vllm, {"title": "Add retry logic", "description": ""})
    assert puts[0]["title"] == GEN_TITLE
    assert puts[0]["description"].startswith("<!-- ai-describe:start -->")


def test_title_matching_a_single_commit_counts_as_auto_generated(monkeypatch, gitlab, vllm):
    puts = run(monkeypatch, gitlab, vllm,
               {"title": "Fix payment bug", "description": ""}, commits=("Fix payment bug",))
    assert puts[0]["title"] == GEN_TITLE


def test_human_written_title_and_description_are_left_alone(monkeypatch, gitlab, vllm):
    puts = run(monkeypatch, gitlab, vllm,
               {"title": "결제 재시도 로직 도입 (RFC-123)", "description": "설계 문서 참고"})
    assert puts == [], "nothing a human wrote may be overwritten"


def test_human_title_is_kept_while_an_empty_description_is_filled(monkeypatch, gitlab, vllm):
    puts = run(monkeypatch, gitlab, vllm,
               {"title": "결제 재시도 로직 도입 (RFC-123)", "description": ""})
    assert "title" not in puts[0]
    assert "description" in puts[0]


def test_label_opt_in_appends_after_existing_text(monkeypatch, gitlab, vllm):
    human = "설계 문서 참고: https://wiki/rfc-123"
    puts = run(monkeypatch, gitlab, vllm,
               {"title": "결제 재시도 로직 도입", "description": human, "labels": ["ai:describe"]})
    assert "title" not in puts[0]
    assert puts[0]["description"].startswith(human)
    assert "<!-- ai-describe:start -->" in puts[0]["description"]


def test_command_in_description_also_opts_in(monkeypatch, gitlab, vllm):
    puts = run(monkeypatch, gitlab, vllm,
               {"title": "결제 재시도 로직 도입", "description": "/ai-describe\n\n기존 설명"})
    assert "기존 설명" in puts[0]["description"]


def test_rerunning_replaces_the_marker_block_instead_of_stacking(monkeypatch, gitlab, vllm):
    first = run(monkeypatch, gitlab, vllm,
                {"title": "T", "description": "기존 설명", "labels": ["ai:describe"]})[0]
    second = run(monkeypatch, gitlab, vllm,
                 {"title": "T", "description": first["description"], "labels": ["ai:describe"]})[0]
    assert second["description"].count("<!-- ai-describe:start -->") == 1
    assert "기존 설명" in second["description"]


def test_draft_prefix_survives_a_title_replacement(monkeypatch, gitlab, vllm):
    puts = run(monkeypatch, gitlab, vllm, {"title": "Draft: Add retry logic", "description": ""})
    assert puts[0]["title"] == f"Draft: {GEN_TITLE}"
