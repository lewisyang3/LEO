"""Action drafting (ROADMAP B7). No network: a fake client stands in for the API."""

import json

import pytest
from anthropic.types import Message

from leo.actions.draft import (
    MAX_ATTEMPTS,
    LLMDrafter,
    TemplateDrafter,
    action_plan,
    build_facts,
    check_body,
    draft_new,
    long_date,
    recipient,
    render,
    template_body,
)
from leo.config import Settings
from leo.fixtures import load_fixture
from leo.llm import LLMOfflineMiss, ResponseCache
from leo.types import ActionState, ActionType

FX = load_fixture()
LEDGER = FX.ledger()
SENDER = FX.customer.name
EXPECTED = {x.finding.id: x for x in FX.expected_findings}
IDS = sorted(EXPECTED)


def _finding(fid: str):
    return EXPECTED[fid].finding


def _text(draft) -> str:
    return draft.subject + "\n" + draft.body


def _message(output, stop_reason: str = "end_turn") -> Message:
    text = output if isinstance(output, str) else json.dumps(output)
    return Message.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-5-5",
            "content": [{"type": "text", "text": text}],
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }
    )


class FakeMessages:
    """Replies in turn from `replies`, repeating the last one."""

    def __init__(self, replies: list[Message]) -> None:
        self.replies = replies
        self.calls: list[dict] = []

    def create(self, **kwargs) -> Message:
        self.calls.append(kwargs)
        return self.replies[min(len(self.calls), len(self.replies)) - 1]


class FakeClient:
    def __init__(self, *replies: Message) -> None:
        self.messages = FakeMessages(list(replies))


def _drafter(tmp_path, *replies, offline=False):
    client = FakeClient(*replies)
    settings = Settings(_env_file=None, leo_llm_offline=offline, anthropic_api_key="x")
    d = LLMDrafter(
        ResponseCache(tmp_path),
        client=client,  # type: ignore[arg-type]
        settings=settings,
        sender=SENDER,
    )
    return d, client


def _good_body(fid: str) -> str:
    """A model-style body that uses every required placeholder and the required words."""
    facts = build_facts(_finding(fid), LEDGER, SENDER)
    words = " ".join(facts.phrases)
    uses = ", ".join(f"{{{r}}}" for r in facts.required)
    return f"Hello {{vendor}} team,\n\nRegarding {uses}. {words}\n\nThank you,\n{{sender}}"


# --- Rules: plan, recipient, facts ----------------------------------------------------------


@pytest.mark.parametrize("fid", IDS)
def test_action_plan_matches_the_key(fid):
    plan, expected = action_plan(_finding(fid)), EXPECTED[fid].expected_action
    assert plan.type == expected.type
    assert plan.alternative == expected.alternative
    assert (plan.pre_step is None) == (expected.pre_step is None)


@pytest.mark.parametrize("fid", IDS)
def test_recipient_matches_the_key(fid):
    f = _finding(fid)
    assert recipient(f, LEDGER, action_plan(f).type) == EXPECTED[fid].expected_action.to


def test_recipient_falls_back_to_contacts_then_empty():
    f = _finding("F1")  # crv_notice has a deliver_to
    ledger = LEDGER.model_copy(deep=True)
    for t in ledger.terms:
        if t.id == "crv_notice":
            t.value.deliver_to = None  # type: ignore[union-attr]
    assert recipient(f, ledger, ActionType.cancellation_notice) == ""  # Corvana has no contacts
    corvana = next(v for v in ledger.vendors if v.id == "corvana")
    lumenpost = next(v for v in ledger.vendors if v.id == "lumenpost")
    corvana.contacts = [
        lumenpost.contacts[0].model_copy(update={"email": "billing@c.example"}),
        lumenpost.contacts[0].model_copy(
            update={"email": "notices@c.example", "purpose": "notices"}
        ),
    ]
    assert recipient(f, ledger, ActionType.cancellation_notice) == "notices@c.example"
    assert recipient(f, ledger, ActionType.billing_dispute) == "billing@c.example"


def test_dates_are_not_zero_padded():
    import datetime

    assert long_date(datetime.date(2027, 1, 1)) == "January 1, 2027"


@pytest.mark.parametrize("fid", IDS)
def test_template_draft_has_every_must_include(fid):
    draft = TemplateDrafter(SENDER).draft(_finding(fid), LEDGER)
    missing = [m for m in EXPECTED[fid].expected_action.must_include if m not in _text(draft)]
    assert missing == []
    assert draft.state == ActionState.draft


@pytest.mark.parametrize("fid", IDS)
def test_template_body_passes_its_own_check(fid):
    f = _finding(fid)
    facts = build_facts(f, LEDGER, SENDER)
    assert check_body(template_body(f, facts), facts) == []


@pytest.mark.parametrize("fid", IDS)
def test_required_placeholders_render_every_must_include(fid):
    """Whatever prose the model writes, using the required placeholders is enough."""
    facts = build_facts(_finding(fid), LEDGER, SENDER)
    body = _good_body(fid)
    assert check_body(body, facts) == []
    text = action_subject(fid) + "\n" + render(body, facts)
    assert [m for m in EXPECTED[fid].expected_action.must_include if m not in text] == []


def action_subject(fid: str) -> str:
    return TemplateDrafter(SENDER).draft(_finding(fid), LEDGER).subject


# --- check_body ------------------------------------------------------------------------------


def _facts():
    return build_facts(_finding("F3"), LEDGER, SENDER)


def test_check_body_flags_problems():
    facts = _facts()
    good = _good_body("F3")
    assert check_body(good, facts) == []
    assert check_body("", facts) == ["the body is empty"]
    assert check_body(None, facts) == ["the body is empty"]
    no_invoices = good.replace("{invoices}", "the invoices")
    assert any("{invoices}" in p for p in check_body(no_invoices, facts))
    assert any("{made_up}" in p for p in check_body(good + " {made_up}", facts))
    assert any("$1,308.00" in p for p in check_body(good + " We paid $1,308.00.", facts))
    assert any("90-day" in p for p in check_body(good + " The 90-day window.", facts))
    one_para = good.replace("\n\n", " ")
    assert any("paragraph" in p for p in check_body(one_para, facts))


def test_check_body_allows_numbered_lists():
    facts = _facts()
    body = _good_body("F3").replace("Thank you", "Please:\n1. Credit us.\n2. Fix it.\n\nThanks")
    assert check_body(body, facts) == []


def test_non_renewal_is_a_required_phrase():
    facts = build_facts(_finding("F1"), LEDGER, SENDER)
    body = _good_body("F1").replace("non-renewal", "cancellation")
    assert any("non-renewal" in p for p in check_body(body, facts))


def test_render_leaves_unknown_placeholders_and_stray_braces():
    facts = _facts()
    assert render("{agreement} {nope} {", facts) == "Agreement No. KCH-MSA-2209 {nope} {"


# --- LLM drafter -----------------------------------------------------------------------------


def test_good_answer_is_rendered(tmp_path):
    d, client = _drafter(tmp_path, _message({"body": _good_body("F3")}))
    draft = d.draft(_finding("F3"), LEDGER)
    assert len(client.messages.calls) == 1
    assert "{" not in draft.body and "INV-KCH-260901" in draft.body
    assert draft.to == "billing@kestrelcloud.example"
    assert draft.state == ActionState.draft
    assert d.records["F3"]["fallback"] is False
    call = client.messages.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert call["output_config"]["format"]["type"] == "json_schema"


def test_retry_with_problems_then_success(tmp_path):
    bad = _message({"body": _good_body("F3").replace("{invoices}", "$1,308.00")})
    d, client = _drafter(tmp_path, bad, _message({"body": _good_body("F3")}))
    draft = d.draft(_finding("F3"), LEDGER)
    calls = client.messages.calls
    assert len(calls) == 2
    first, second = (c["messages"][0]["content"] for c in calls)
    assert "previous draft had these problems" not in first
    assert "previous draft had these problems" in second and "{invoices}" in second
    assert d.records["F3"]["fallback"] is False
    assert "INV-KCH-260901" in draft.body


def test_every_attempt_fails_then_template(tmp_path):
    bad = _message({"body": "Hello,\n\nPlease fix the bill."})
    d, client = _drafter(tmp_path, bad)
    draft = d.draft(_finding("F5"), LEDGER)
    assert len(client.messages.calls) == MAX_ATTEMPTS
    assert d.records["F5"]["fallback"] is True
    assert draft.body == TemplateDrafter(SENDER).draft(_finding("F5"), LEDGER).body


def test_cut_off_answer_goes_straight_to_template(tmp_path):
    d, client = _drafter(tmp_path, _message('{"body": "Hel', stop_reason="max_tokens"))
    draft = d.draft(_finding("F4"), LEDGER)
    assert len(client.messages.calls) == 1
    assert d.records["F4"]["fallback"] is True
    assert "QS-OF-1187" in draft.body
    assert draft.pre_step is not None


def test_rerun_replays_from_cache(tmp_path):
    bad = _message({"body": "Hello,\n\nPlease fix the bill."})
    d, client = _drafter(tmp_path, bad, _message({"body": _good_body("F2")}))
    first = d.draft(_finding("F2"), LEDGER)
    assert len(client.messages.calls) == 2
    d2, client2 = _drafter(tmp_path, bad)
    assert d2.draft(_finding("F2"), LEDGER) == first
    assert client2.messages.calls == []


def test_offline_miss_raises(tmp_path):
    d, client = _drafter(tmp_path, _message({"body": _good_body("F1")}), offline=True)
    with pytest.raises(LLMOfflineMiss):
        d.draft(_finding("F1"), LEDGER)
    assert client.messages.calls == []


def test_draft_new_skips_existing_actions(tmp_path):
    d, client = _drafter(tmp_path, _message({"body": "Hello,\n\nbad"}))
    findings = [_finding("F1"), _finding("F2")]
    existing = {("F1", ActionType.cancellation_notice), ("F2", ActionType.negotiation_email)}
    assert draft_new(d, findings, LEDGER, existing) == []
    assert client.messages.calls == []
    # A different type for the same finding doesn't count as existing.
    out = draft_new(TemplateDrafter(SENDER), findings, LEDGER, {("F1", ActionType.billing_dispute)})
    assert [x.finding for x in out] == ["F1", "F2"]


# --- Committed cache -------------------------------------------------------------------------


def test_cached_drafts_pass_the_key():
    from leo.eval.runner import run_drafts

    settings = Settings(_env_file=None, leo_llm_offline=True)
    if not list((settings.leo_llm_cache_dir / "draft").glob("*.json")):
        pytest.skip("no committed draft cache")
    d = LLMDrafter(ResponseCache(settings.leo_llm_cache_dir), settings=settings, sender=SENDER)
    scores = run_drafts(FX, d)
    assert [(s.finding, s.error) for s in scores if s.error] == []
    assert all(s.ok for s in scores), [s for s in scores if not s.ok]
    assert not any(s.fallback for s in scores)


def test_check_body_allows_quotes_only_around_clauses():
    facts = _facts()
    good = _good_body("F3")
    assert check_body(good + ' The agreement states: "{cap_clause}"', facts) == []
    problems = check_body(good + ' Per "{agreement}".', facts)
    assert any("{agreement}" in p and "quotation" in p for p in problems)


@pytest.mark.parametrize("fid", IDS)
def test_only_notice_and_price_drafts_get_the_deadline(fid):
    facts = build_facts(_finding(fid), LEDGER, SENDER)
    assert ("deadline" in facts.values) == (fid in {"F1", "F2"})


def test_check_body_flags_words_repeated_before_a_placeholder():
    facts = _facts()  # {agreement} is "Agreement No. KCH-MSA-2209"
    good = _good_body("F3")
    assert check_body(good + " Under {agreement}.", facts) == []
    problems = check_body(good + " Under Agreement No. {agreement}.", facts)
    assert any("repeat" in p and "{agreement}" in p for p in problems)
