from pathlib import Path
from email import policy
from email.parser import BytesParser
from typesafe_sdk import Choice, TypeSafeClient
from dotenv import load_dotenv
import csv

load_dotenv()

INBOX = Path("data/synthetic/inbox")
results = []

def read_email(path):
   with open(path, "rb") as f:
      msg = BytesParser(policy=policy.default).parse(f)

   body = msg.get_body(preferencelist=("plain",))
   text = body.get_content() if body else ""

   return f"""
From: {msg.get('From')}
To: {msg.get('To')}
Subject: {msg.get('Subject')}

{text}
"""

with TypeSafeClient(model="jev-latest") as client:
   for path in sorted(INBOX.glob("*.eml")):
      ticket = read_email(path)

      try:
         response = client.system_one(
               state=ticket,
               questions={
                  "category": Choice(
                     instructions="Classify this email as billing or non-billing.",
                     criteria={
                           "billing": (
                              "Email primarily concerns invoices, "
                              "payments, charges, refunds, or billing disputes."
                           ),
                           "non_billing": (
                              "Email does not primarily concern billing. "
                              "Includes technical, sales, operational, "
                              "and general correspondence."
                           ),
                     },
                  )
               },
         )

         prediction = response.choices["category"].probabilities["billing"]

      except Exception as e:
         print(f"Error processing {path.name}: {e}")
         prediction = 0

      if prediction > 0.65:
         jevStrPrediction = response.choices["category"].choice
         results.append({"filename": path.name, "prediction": jevStrPrediction,})

      print(f"{path.name}: {prediction}")

with open("billing_predictions.csv", "w", newline="") as f:
   writer = csv.DictWriter(
      f, fieldnames=["filename", "prediction"]
   )
   writer.writeheader()
   writer.writerows(results)