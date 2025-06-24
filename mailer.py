# mailer.py
from msal import SerializableTokenCache
from obo import register_user_for_obo
from config import CLIENT_ID, AUTHORITY, CLIENT_SECRET, GRAPH_SCOPES
import requests

def send_weekly_email(uid: str, recipients: list[str]):
    # 1) Ensure OBO registration happened (once per user)
    if not register_user_for_obo(uid):
        return

    # 2) Rehydrate the MSAL cache from memory (it’s already in msal_app.token_cache)
    #    and silent-acquire a fresh Graph token
    from obo import msal_app
    result = msal_app.acquire_token_silent(GRAPH_SCOPES, account=None)
    if not result or "access_token" not in result:
        print("❌ Silent token acquisition failed")
        return
    graph_token = result["access_token"]

    # 3) Call Graph
    for addr in recipients:
        payload = {
            "message": {
                "subject": "Weekly Questions",
                "body": {"contentType": "Text", "content": "How was your week?"},
                "toRecipients": [{"emailAddress": {"address": addr}}]
            },
            "saveToSentItems": True
        }
        resp = requests.post(
            "https://graph.microsoft.com/v1.0/me/sendMail",
            headers={
                "Authorization": f"Bearer {graph_token}",
                "Content-Type": "application/json"
            },
            json=payload
        )
        print(f"Sent to {addr}: {resp.status_code}")
