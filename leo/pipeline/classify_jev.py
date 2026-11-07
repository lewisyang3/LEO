import json
import os

from dotenv import load_dotenv
from typesafe_sdk import Noul, NoulCriteria, TypeSafeClient

from leo.interfaces import Classification, DocumentText
from leo.types import DocumentClass

load_dotenv()

results = []

QUESTIONS = {
    "invoice": Noul(
        instructions="Is this document an invoice?",
        criteria=NoulCriteria(
            true="Requests payment for goods or services already delivered. States an amount due.",
            false="Does not request payment for delivered goods or services.",
        ),
    ),
    "receipt": Noul(
        instructions="Is this document a receipt?",
        criteria=NoulCriteria(
            true="Confirms that a payment was already received. Nothing is owed.",
            false="Does not confirm a received payment, or requests payment.",
        ),
    ),
    "price_notice": Noul(
        instructions="Is this document a price change notice?",
        criteria=NoulCriteria(
            true="Announces that prices, fees, or rates will change on a future date.",
            false="Does not announce a future price change.",
        ),
    ),
    "order_form": Noul(
        instructions="Is this document an order form?",
        criteria=NoulCriteria(
            true="A request or commitment to purchase specific products, quantities, or plans.",
            false="Not a purchase request or commitment.",
        ),
    ),
    "contract": Noul(
        instructions="Is this document the governing agreement?",
        criteria=NoulCriteria(
            true="States terms, term length, renewal, notice, and liability.",
            false="Not a governing agreement.",
        ),
    ),
    "noise": Noul(
        instructions="Is this unrelated to vendor billing, pricing, or agreements?",
        criteria=NoulCriteria(
            true="Technical, sales, or general correspondence.",
            false="Concerns vendor billing, pricing, or agreements.",
        ),
    ),
}

REAL = ["invoice", "receipt", "price_notice", "order_form", "contract"]


class JevClassifier:
    def __init__(self) -> None:
        key = (os.environ.get("JEV_API_KEY") or "").strip()
        if not key:
            raise RuntimeError("JEV_API_KEY is not set in .env")
        self.client = TypeSafeClient(api_key=key, model="jev-latest")

    def classify(self, doc: DocumentText) -> Classification:

        response = self.client.system_one(state=doc.text, questions=QUESTIONS)
        probs = {name: response.answers[name].noul for name in response.answers}
        best = max(REAL, key=lambda c: probs[c])

        if probs["noise"] >= 0.7 and probs[best] < 0.3:
            doc_class, confidence = DocumentClass.noise, probs["noise"]
        else:
            doc_class, confidence = DocumentClass(best), probs[best]

        return Classification(
            doc_class=doc_class,
            confidence=confidence,
            engine="jev",
            reason=json.dumps(probs),
        )


"""
def read_email(path):
    with open(path, "rb") as f:
        msg = BytesParser(policy=policy.default).parse(f)

    body = msg.get_body(preferencelist=("plain",))
    text = body.get_content() if body else ""

    return f"From: {msg.get('From')}\nTo: {msg.get('To')}\nSubject: {msg.get('Subject')}\n\n{text}"


def classify(client, text) -> Classification:
    response = client.system_one(state=text, questions=QUESTIONS)
    potClassifier, potPrediction = max(
        response.answers.items(), key=lambda noulTuple: noulTuple[1].noul
    )
    print(potClassifier, potPrediction.noul)
    if potPrediction.noul > 0.7:
        doc_class = DocumentClass(potClassifier)
        return Classification(
            doc_class=doc_class,
            confidence=potPrediction.noul,
            engine="jev",
            reason=str(response.answers),
        )

with TypeSafeClient(model="jev-latest") as client:
    for path in sorted(INBOX.glob("*.eml")):
        ticket = read_email(path)

        try:
            c = classify(client, ticket)
            results.append(
                {
                    "filename": path.name,
                    "doc_class": c.doc_class.value,
                    "confidence": c.confidence,
                    "probs": c.reason,
                    "error": "",
                }
            )

        except Exception as e:
            print(f"Error processing {path.name}: {e}")
            results.append(
                {
                    "filename": path.name,
                    "doc_class": "unclassified",
                    "confidence": "",
                    "probs": "",
                    "error": str(e),
                }
            )

with open("billing_predictions.csv", "w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=["filename", "doc_class", "confidence", "probs", "error"])
    writer.writeheader()
    writer.writerows(results)
"""
