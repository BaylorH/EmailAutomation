# EmailAutomation.py

from msal import PublicClientApplication
import requests

CLIENT_ID = "70013c0d-89a5-40b1-9dc9-4da89dd0362e"
AUTHORITY = "https://login.microsoftonline.com/common"
SCOPES = ["Mail.Read", "Mail.ReadWrite", "Mail.Send"]

# Create an MSAL app instance
app = PublicClientApplication(CLIENT_ID, authority=AUTHORITY)

# Login with a browser popup
result = app.acquire_token_interactive(scopes=SCOPES)
access_token = result["access_token"]
headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

# Logic to send a question email
def send_question_email(to_address):

    # Conents of email
    email_data = {
        "message": {
            "subject": "Weekly Questions",
            "body": {
                "contentType": "Text",
                "content": (
                    "Hi,\n\nPlease answer the following:\n"
                    "1. How was your week?\n"
                    "2. What challenges did you face?\n"
                    "3. Any updates to share?\n\nThanks!"
                ),
            },
            "toRecipients": [{"emailAddress": {"address": to_address}}],
        },
        "saveToSentItems": "true"
    }

    # Send email
    res = requests.post(
        "https://graph.microsoft.com/v1.0/me/sendMail",
        headers=headers,
        json=email_data
    )
    print("Email sent:", res.status_code)

# Check inbox for replies and save them to Excel
def extract_replies_to_excel():
    res = requests.get(
        "https://graph.microsoft.com/v1.0/me/messages?$orderby=receivedDateTime desc&$top=5",
        headers=headers
    )
    messages = res.json().get("value", [])
    for msg in messages:
        if "Weekly Questions" in msg["subject"] and not msg["isRead"]:
            sender = msg["from"]["emailAddress"]["address"]
            content = msg["body"]["content"]

            import openpyxl
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.append(["Sender", "Response"])
            ws.append([sender, content])
            wb.save("responses.xlsx")
            print(f"Saved reply from {sender}")
            return

send_question_email("bp21harrison@gmail.com") 
extract_replies_to_excel()
