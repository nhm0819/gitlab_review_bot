"""vLLM transport: Qwen thinking blocks and the exact sampling payload sent."""
import pytest

from review_bot.describe import MRDescriber
from review_bot.llm import LLMSettings, strip_thinking
from review_bot.reviewer import VLLMReviewer

from conftest import RecordingHandler, chat_completion, json_response, read_json, serve


def test_strips_reasoning_block():
    assert strip_thinking('<think>reasoning</think>\n\n{"a":1}') == '{"a":1}'


def test_passes_through_when_no_reasoning_block():
    assert strip_thinking('{"a":1}') == '{"a":1}'


def test_uses_the_final_block_when_several_are_present():
    assert strip_thinking('<think>a</think>x<think>b</think>{"c":2}') == '{"c":2}'


def test_truncated_reasoning_raises_an_actionable_error():
    with pytest.raises(ValueError, match="VLLM_MAX_TOKENS"):
        strip_thinking("<think>cut off mid thought")


def spawn(answer):
    captured = []

    class Handler(RecordingHandler):
        def do_POST(self):
            body = read_json(self)
            captured.append(body)
            json_response(self, chat_completion(body["model"], answer))

    url, _ = serve(Handler)
    return url, captured


REVIEW_ANSWER = {"summary": "ok", "comments": [
    {"file": "a.py", "line": 1, "severity": "bug", "comment": "x"}]}
FILE_BLOCKS = [{"path": "a.py", "diff": "@@ -1 +1 @@\n-x\n+y", "commentable_lines": [1]}]


def test_review_parses_through_a_thinking_block():
    url, _ = spawn(REVIEW_ANSWER)
    result = VLLMReviewer(LLMSettings(base_url=url + "/v1")).review("t", "d", "", FILE_BLOCKS)
    assert result.summary == "ok"
    assert result.comments[0].line == 1


def test_sampling_parameters_match_the_model_card_preset():
    url, captured = spawn(REVIEW_ANSWER)
    VLLMReviewer(LLMSettings(base_url=url + "/v1")).review("t", "d", "", FILE_BLOCKS)
    sent = captured[0]
    assert sent["model"] == "Qwen/Qwen3.6-35B-A3B-FP8"
    assert sent["max_tokens"] == 32768
    assert sent["temperature"] == 0.6
    assert sent["top_p"] == 0.95
    assert sent["presence_penalty"] == 0.0
    assert sent["top_k"] == 20, "top_k is not a standard field and must ride in extra_body"


def test_thinking_enabled_does_not_send_the_disable_flag():
    url, captured = spawn(REVIEW_ANSWER)
    VLLMReviewer(LLMSettings(base_url=url + "/v1")).review("t", "d", "", FILE_BLOCKS)
    assert "chat_template_kwargs" not in captured[0]


def test_thinking_disabled_sends_the_documented_flag():
    url, captured = spawn(REVIEW_ANSWER)
    settings = LLMSettings(base_url=url + "/v1", enable_thinking=False)
    VLLMReviewer(settings).review("t", "d", "", FILE_BLOCKS)
    assert captured[0]["chat_template_kwargs"] == {"enable_thinking": False}


def test_describer_inherits_the_same_token_budget():
    """A small max_tokens here would be consumed by thinking before any answer."""
    url, captured = spawn({"title": "T", "description": "D"})
    settings = LLMSettings(base_url=url + "/v1")
    MRDescriber(settings, language="Korean").describe("f", "main", ["c"], [{"path": "a.py", "diff": "d"}])
    assert captured[0]["max_tokens"] == 32768
    assert "Korean" in captured[0]["messages"][0]["content"]


def test_malformed_json_with_surrounding_prose_still_parses():
    url, _ = spawn(REVIEW_ANSWER)
    result = VLLMReviewer(LLMSettings(base_url=url + "/v1")).review("t", "d", "", FILE_BLOCKS)
    assert result.comments
