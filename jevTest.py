from typesafe_sdk import Choice, Noul, Score, TypeSafeClient
from dotenv import load_dotenv
load_dotenv()

ticket = """Dear Valued Partner,
We are writing to inform you of an upcoming change to our pricing structure for our enterprise software licenses and managed support services. Effective January 1, 2027, the standard rate for these services will increase by 6%.
This adjustment is necessary to account for the rising operational costs of maintaining our secure cloud infrastructure and to support our continued investment in platform reliability and feature updates.
What this means for you:
• Current Contracts: All active, multi-year contracts will honor their existing locked-in rates until their scheduled renewal date.
• Upcoming Renewals: Any subscriptions or renewals processed on or after January 1, 2027, will be billed at the updated rate.
• Invoices: Your account dashboard will update automatically to reflect these changes on your next billing cycle following the effective date.
We deeply appreciate your business and remain committed to delivering the highest level of service and performance for your team. A full, updated price sheet is attached to this email for your records.
If you have any questions regarding your specific account or upcoming renewals, please do not hesitate to reach out directly to your account manager or reply to this message.
Sincerely,
Sarah Jenkins
VP of Customer Success
Apex Cloud Solutions"""

#IMPORTANT: Jev "Choices" require that criteria must be strictly mutually exclusive.

with TypeSafeClient(model="jev-latest") as client:  # Reads TYPESAFE_API_KEY.
   response = client.system_one(
      state=ticket,
      questions={
         "department": Choice(
            instructions="What kind of email is this?",
            criteria={
               "billing": "Payment or subscription issues",
               "technical": "Bugs or integration problems",
               "sales": "Pricing or account questions",
            }
         )
      }
   )

print(response.choices["department"].probabilities)