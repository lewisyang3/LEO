"""The classifier. No network: a fake client stands in for Jev, except in the
opt-in live test."""

import json
import os
from types import SimpleNamespace

import pytest
from typesafe_sdk import TypeSafeError

from leo.config import Settings
from leo.eval import runner
from leo.eval.scoring import DocClassification, threshold_sweep
from leo.fixtures import load_fixture
from leo.interfaces import Classifier, DocumentText
from leo.jev import JevUnavailable, jev_client
from leo.pipeline.classify import (
    JEV_CRITERIA,
    JEV_QUESTION,
    ClassificationFailed,
    JevClassifier,
)
from leo.types import DocumentClass
from leo.types import SourceDocument as Doc

FX = load_fixture()
TEXT = "From: Lumenpost\nSubject: Your price is changing\n\nGrowth goes to $339/month.\n"
PATH = "inbox/x.eml"


def _doc(text: str = TEXT, path: str = PATH) -> DocumentText:
    return DocumentText(document=Doc(path=path, doc_class=None, vendor=None), text=text)


def _probs(**kw: float) -> dict[str, float]:
    """Probabilities over all six classes; the rest share what kw leaves."""
    rest = [c.value for c in DocumentClass if c.value not in kw]
    left = (1.0 - sum(kw.values())) / len(rest) if rest else 0.0
    return {**dict.fromkeys(rest, left), **kw}


class FakeJev:
    def __init__(self, probabilities=None, error=None, question=JEV_QUESTION):
        self.calls = []
        self.probabilities = probabilities or _probs(price_notice=0.9)
        self.error = error
        self.question = question

    def system_one(self, state, questions):
        self.calls.append((state, questions))
        if self.error:
            raise self.error
        choice = max(self.probabilities, key=lambda k: self.probabilities[k])
        answer = SimpleNamespace(choice=choice, probabilities=self.probabilities)
        return SimpleNamespace(choices={self.question: answer})


def _classify(threshold=0.65, **kw):
    fake = FakeJev(**kw)
    clf = JevClassifier(fake, threshold=threshold)
    return fake, clf, clf.classify(_doc())


def test_is_a_classifier():
    assert isinstance(JevClassifier(FakeJev()), Classifier)


def test_one_call_with_text_state_and_six_classes():
    fake, _, _ = _classify()
    assert len(fake.calls) == 1
    state, questions = fake.calls[0]
    assert state == TEXT
    assert set(questions) == {JEV_QUESTION}
    assert set(JEV_CRITERIA) == {c.value for c in DocumentClass}


@pytest.mark.parametrize("cls", [c for c in DocumentClass if c != DocumentClass.noise])
def test_top_non_noise_class_is_kept(cls):
    _, _, c = _classify(probabilities=_probs(**{cls.value: 0.8, "noise": 0.1}))
    assert c.doc_class == cls and c.confidence == pytest.approx(0.8) and c.engine == "jev"


def test_noise_at_threshold_is_noise():
    _, _, c = _classify(threshold=0.65, probabilities=_probs(noise=0.65))
    assert c.doc_class == DocumentClass.noise and c.confidence == pytest.approx(0.65)


def test_noise_below_threshold_keeps_best_non_noise_class():
    # Noise is Jev's top choice, but not confident enough: the document is kept.
    probs = _probs(noise=0.6, invoice=0.3)
    _, _, c = _classify(threshold=0.65, probabilities=probs)
    assert c.doc_class == DocumentClass.invoice and c.confidence == pytest.approx(0.3)


def test_probabilities_are_recorded():
    probs = _probs(noise=0.2, contract=0.7)
    _, clf, c = _classify(probabilities=probs)
    assert clf.records == {PATH: probs}
    assert json.loads(c.reason) == probs


@pytest.mark.parametrize(
    "kw",
    [
        {"error": TypeSafeError("rate limited")},
        {"question": "something_else"},
        {"probabilities": {"invoice": 0.9}},  # no noise probability
        {"probabilities": {"noise": 0.1, "spam": 0.9}},  # no known non-noise class
    ],
)
def test_error_raises_never_noise(kw):
    clf = JevClassifier(FakeJev(**kw))
    with pytest.raises(ClassificationFailed):
        clf.classify(_doc())
    assert clf.records == {}


def test_error_is_an_eval_error_not_noise(monkeypatch):
    monkeypatch.setitem(
        runner.CLASSIFIERS,
        "jev-fake",
        lambda fixture: JevClassifier(FakeJev(error=TypeSafeError("down"))),
    )
    paths = ["inbox/01_lumenpost_price_change_notice.eml", "inbox/04_lumenpost_newsletter.eml"]
    result = runner.run(FX, classifier="jev-fake", docs=paths)
    assert [d.predicted for d in result.classification] == [None, None]
    assert all("ClassificationFailed" in (d.error or "") for d in result.classification)
    assert result.classification_summary.matters_recall.hit == 0
    assert result.classification_summary.noise_false_pass.hit == 0


def test_eval_records_probabilities_and_sweep(monkeypatch):
    probs = _probs(noise=0.3, price_notice=0.6)
    monkeypatch.setitem(
        runner.CLASSIFIERS, "jev-fake", lambda fixture: JevClassifier(FakeJev(probs))
    )
    result = runner.run(FX, classifier="jev-fake")
    assert len(result.classification) == 22
    assert all(d.probabilities == probs for d in result.classification)
    cs = result.classification_summary
    assert cs.threshold_sweep and cs.recommended_threshold == 0.5
    report = runner.format_report(result)
    assert "noise threshold sweep" in report and "p(noise)" in report


def _scored(path, expected, matters, p_noise):
    probs = None if p_noise is None else {"noise": p_noise, "invoice": 1 - p_noise}
    return DocClassification(
        path=path,
        expected=expected,
        predicted=None,
        confidence=None,
        matters=matters,
        probabilities=probs,
    )


def test_threshold_sweep_recommends_lowest_full_recall():
    docs = [
        _scored("a", DocumentClass.invoice, True, 0.10),
        _scored("b", DocumentClass.contract, True, 0.55),  # the highest matters P(noise)
        _scored("c", DocumentClass.noise, False, 0.52),
        _scored("d", DocumentClass.noise, False, 0.90),
    ]
    sweep, rec = threshold_sweep(docs)
    by_t = {p.threshold: p for p in sweep}
    assert rec == 0.6  # first candidate above 0.55
    assert (by_t[0.55].matters_recall.hit, by_t[0.55].matters_recall.total) == (1, 2)
    assert (by_t[0.6].noise_false_pass.hit, by_t[0.6].noise_false_pass.total) == (1, 2)
    assert by_t[0.95].noise_false_pass.hit == 2
    assert [p.threshold for p in sweep] == sorted(by_t)


def test_threshold_sweep_no_recommendation_when_a_matters_doc_errored():
    docs = [
        _scored("a", DocumentClass.invoice, True, None),
        _scored("b", DocumentClass.noise, False, 0.9),
    ]
    sweep, rec = threshold_sweep(docs)
    assert sweep and rec is None


def test_threshold_sweep_empty_without_probabilities():
    assert threshold_sweep([_scored("a", DocumentClass.invoice, True, None)]) == ([], None)


def test_jev_registered():
    assert "jev" in runner.CLASSIFIERS


@pytest.mark.parametrize("key", [None, "", "  "])
def test_missing_jev_key_fails_fast(key):
    with pytest.raises(JevUnavailable, match="JEV_API_KEY"):
        jev_client(Settings(_env_file=None, jev_api_key=key))


# --- Live Jev (opt-in) -----------------------------------------------------------------------

LIVE = os.environ.get("LEO_LIVE") == "1"


@pytest.mark.skipif(not LIVE, reason="set LEO_LIVE=1 and JEV_API_KEY to call Jev")
def test_live_jev_keeps_planted_and_drops_newsletter():
    clf = JevClassifier(jev_client())
    for path in (
        "inbox/01_lumenpost_price_change_notice.eml",
        "inbox/03_lumenpost_order_confirmation.eml",
    ):
        assert clf.classify(FX.document_text(path)).doc_class != DocumentClass.noise, path
    news = clf.classify(FX.document_text("inbox/04_lumenpost_newsletter.eml"))
    assert news.doc_class == DocumentClass.noise
