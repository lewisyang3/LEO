"""Stage 1: the classifier (ROADMAP B2). Jev only, with no LLM step.

`JevClassifier` makes one Jev call per document, as a choice over the six `DocumentClass` labels.
Jev's probability for `noise` alone decides noise: at or above `NOISE_THRESHOLD` the document is
noise, and below it the document gets its most probable non-noise class. So a document Jev is
unsure about is kept, never dropped.

A failed Jev call raises `ClassificationFailed`. It never becomes noise. The worker keeps that
document unclassified for review, and the eval records it as an error.

Every classification carries Jev's probabilities (in `reason`, and in `records` for the eval),
so the noise labels can be spot-checked and the threshold tuned.
"""

import json
import logging
from typing import Any

from typesafe_sdk import Choice, TypeSafeError

from leo.analytics.usage import record_usage
from leo.interfaces import Classification, DocumentText
from leo.types import DocumentClass

log = logging.getLogger(__name__)

JEV_QUESTION = "doc_class"
JEV_INSTRUCTIONS = (
    "Classify this document, received by a small business, by what it means for the business's "
    "vendor contracts and spending. Anything that states or changes vendor terms, prices or "
    "charges is not noise."
)
_CRITERIA: dict[DocumentClass, str] = {
    DocumentClass.contract: (
        "A vendor agreement, master services agreement, subscription agreement or terms of "
        "service, with terms such as renewal, notice, price, price caps or termination."
    ),
    DocumentClass.order_form: (
        "An order form, or an order or plan confirmation, that sets plans, quantities, seats, "
        "prices or terms."
    ),
    DocumentClass.price_notice: (
        "A notice from a vendor that a price, plan or term is changing, including one forwarded "
        "by a colleague."
    ),
    DocumentClass.invoice: (
        "A bill or invoice requesting payment, including a renewal invoice or a resent copy."
    ),
    DocumentClass.receipt: "A receipt or payment confirmation for a purchase already paid.",
    DocumentClass.noise: (
        "Marketing, newsletters, product news, event invites, webinars, cold sales outreach or "
        "internal announcements, with no vendor terms, prices or charges for this business."
    ),
}
JEV_CRITERIA: dict[str, str] = {k.value: v for k, v in _CRITERIA.items()}
# Tuned 2026-11-05 (ROADMAP B2): every document that matters scored P(noise) ~0.00, and the
# lowest noise document 0.84, so any value in (0.00, 0.84] keeps 9/9 with 0/5 false-pass.
# 0.5 leaves a wide margin on both sides.
NOISE_THRESHOLD = 0.5
ENGINE = "jev"


class ClassificationFailed(RuntimeError):
    """Jev gave no usable answer. The document stays unclassified; it is never noise."""


class JevClassifier:
    """The `Classifier` for stage 1."""

    def __init__(self, client: Any, threshold: float = NOISE_THRESHOLD) -> None:
        self.client = client  # a typesafe_sdk.TypeSafeClient (`leo.jev.jev_client`)
        self.threshold = threshold
        # path -> Jev's probabilities, for the eval's threshold sweep
        self.records: dict[str, dict[str, float]] = {}

    def classify(self, doc: DocumentText) -> Classification:
        path = doc.document.path
        try:
            resp = self.client.system_one(
                state=doc.text,
                questions={
                    JEV_QUESTION: Choice(instructions=JEV_INSTRUCTIONS, criteria=JEV_CRITERIA)
                },
            )
            record_usage(
                "jev", getattr(resp, "model", "jev-latest"), getattr(resp, "usage", None)
            )
            probabilities = {
                str(k): float(v) for k, v in resp.choices[JEV_QUESTION].probabilities.items()
            }
            p_noise = probabilities[DocumentClass.noise.value]
        except (TypeSafeError, KeyError) as e:
            if isinstance(e, TypeSafeError):
                record_usage("jev", "jev-latest", status="failed")
            log.warning("Jev classification failed for %s: %r", path, e)
            raise ClassificationFailed(f"Jev classification failed: {e!r}") from e

        result = decide(probabilities, p_noise, self.threshold)
        self.records[path] = probabilities
        return result


def decide(probabilities: dict[str, float], p_noise: float, threshold: float) -> Classification:
    reason = json.dumps(probabilities, sort_keys=True)
    if p_noise >= threshold:
        return Classification(
            doc_class=DocumentClass.noise, confidence=p_noise, engine=ENGINE, reason=reason
        )
    kept = {k: p for k, p in probabilities.items() if k in DocumentClass._value2member_map_}
    kept.pop(DocumentClass.noise, None)
    if not kept:
        raise ClassificationFailed(f"Jev returned no non-noise class: {reason}")
    best = max(kept, key=lambda k: kept[k])
    return Classification(
        doc_class=DocumentClass(best), confidence=kept[best], engine=ENGINE, reason=reason
    )
