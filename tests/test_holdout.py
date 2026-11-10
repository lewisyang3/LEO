"""The held-out classification set (B2). No network: a fake client stands in for Jev."""

from types import SimpleNamespace

from typesafe_sdk import TypeSafeError

from leo.eval.holdout import HOLDOUT_DIR, format_holdout, load_holdout, run_holdout
from leo.pipeline.classify import JEV_QUESTION, JevClassifier
from leo.types import DocumentClass


class FakeJev:
    def __init__(self, probabilities=None, error=None):
        self.probabilities = probabilities
        self.error = error
        self.calls = 0

    def system_one(self, state, questions):
        self.calls += 1
        if self.error:
            raise self.error
        answer = SimpleNamespace(probabilities=self.probabilities)
        return SimpleNamespace(choices={JEV_QUESTION: answer})


def _probs(**kw):
    rest = [c.value for c in DocumentClass if c.value not in kw]
    left = (1.0 - sum(kw.values())) / len(rest)
    return {**dict.fromkeys(rest, left), **kw}


def test_labels_resolve():
    h = load_holdout()
    assert len(h.documents) == 5
    assert all((HOLDOUT_DIR / d.path).is_file() for d in h.documents)
    assert len({d.path for d in h.documents}) == 5
    classes = {d.doc_class for d in h.documents}
    assert DocumentClass.noise in classes and len(classes) > 1
    # Stages see the document unclassified.
    assert all(d.text.document.doc_class is None and d.text.text for d in h.documents)


def test_run_records_probabilities():
    fake = FakeJev(_probs(invoice=0.9))
    result = run_holdout(load_holdout(), JevClassifier(fake), "jev-fake")
    assert fake.calls == 5
    rows = result.classification
    assert [d.predicted for d in rows] == [DocumentClass.invoice] * 5
    assert all(d.probabilities and d.p_noise is not None for d in rows)
    cs = result.classification_summary
    assert cs.matters_recall.hit == cs.matters_recall.total == 3
    assert cs.noise_false_pass.hit == cs.noise_false_pass.total == 2
    assert "not noise in the key" in format_holdout(result)


def test_jev_error_is_never_noise():
    fake = FakeJev(error=TypeSafeError("down"))
    result = run_holdout(load_holdout(), JevClassifier(fake), "jev-fake")
    assert all(d.predicted is None for d in result.classification)
    assert all("ClassificationFailed" in (d.error or "") for d in result.classification)
    assert result.classification_summary.matters_recall.hit == 0
