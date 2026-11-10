"""`structured_call`: the default client keeps the SDK's retries, and every outbound call is
recorded for B6, including one that fails."""

from decimal import Decimal

import anthropic
import httpx2
import pytest
from anthropic.types import Message

from leo import llm
from leo.analytics.usage import UsageCall, collect_usage, estimated_cost
from leo.llm import ResponseCache, structured_call


def _message(text: str) -> Message:
    return Message.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-5-5",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }
    )


class FakeMessages:
    """Replies with `reply`, or with `reply(kwargs)` when it is a function."""

    def __init__(self, reply) -> None:
        self.reply = reply
        self.calls: list[dict] = []

    def create(self, **kwargs) -> Message:
        self.calls.append(kwargs)
        return self.reply(kwargs) if callable(self.reply) else self.reply


class FakeClient:
    def __init__(self, reply) -> None:
        self.messages = FakeMessages(reply)


@pytest.fixture(autouse=True)
def _no_prices(monkeypatch):
    """No rate configured, so cost is None whatever the shell exports."""
    import os

    for name in [n for n in os.environ if n.startswith("LEO_B6_")]:
        monkeypatch.delenv(name)


def _call(tmp_path, client=None):
    return structured_call(
        cache=ResponseCache(tmp_path),
        stage="test",
        doc_hash="0" * 64,
        model="claude-haiku-5-5",
        prompt_version="v1",
        effort="low",
        system="system",
        user="user",
        schema={"type": "object"},
        client=client,
        api_key="sk-test",
    )


def test_default_client_keeps_sdk_retries(tmp_path, monkeypatch):
    """The worker's 3 quick attempts don't replace the SDK's backoff on 429/529/5xx."""
    built: list[dict] = []

    def fake_anthropic(**kwargs):
        built.append(kwargs)
        return FakeClient(_message('{"a": 1}'))

    monkeypatch.setattr(llm.anthropic, "Anthropic", fake_anthropic)
    with collect_usage() as calls:
        result = _call(tmp_path)

    assert result.output == {"a": 1}
    assert built == [{"api_key": "sk-test"}]  # no max_retries override
    assert calls == [UsageCall("claude", "claude-haiku-5-5", 10, 20, None, "success")]


def test_rate_limit_is_recorded_and_raised(tmp_path):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    error = anthropic.RateLimitError(
        "rate limited", response=httpx2.Response(429, request=request), body=None
    )

    def raise_(_kwargs):
        raise error

    with collect_usage() as calls, pytest.raises(anthropic.RateLimitError):
        _call(tmp_path, FakeClient(raise_))

    assert calls == [UsageCall("claude", "claude-haiku-5-5", None, None, None, "rate_limited")]


def test_cache_tokens_are_priced_at_their_own_rates(monkeypatch):
    for kind, rate in (
        ("INPUT", "1"),
        ("OUTPUT", "5"),
        ("CACHE_WRITE", "1.25"),
        ("CACHE_READ", "0.1"),
    ):
        monkeypatch.setenv(f"LEO_B6_CLAUDE_{kind}_PER_M", rate)
    # 450 + 900*5 + 1000*1.25 + 5000*0.1 = 6700 per million
    assert estimated_cost(
        "claude", "claude-haiku-5-5", 450, 900, cache_write=1000, cache_read=5000
    ) == Decimal("0.00670000")


def test_cache_tokens_without_a_rate_are_unpriced_not_free(monkeypatch):
    monkeypatch.setenv("LEO_B6_CLAUDE_INPUT_PER_M", "1")
    monkeypatch.setenv("LEO_B6_CLAUDE_OUTPUT_PER_M", "5")
    assert estimated_cost("claude", "m", 450, 900, cache_read=5000) is None
    assert estimated_cost("claude", "m", 450, 900, cache_write=0, cache_read=0) == Decimal(
        "0.00495000"
    )
