# obo.py
from msal import ConfidentialClientApplication
from config import CLIENT_ID, CLIENT_SECRET, AUTHORITY, GRAPH_SCOPES, db

# build your confidential client once
msal_app = ConfidentialClientApplication(
    CLIENT_ID,
    authority=AUTHORITY,
    client_credential=CLIENT_SECRET
)

def register_user_for_obo(uid: str) -> bool:
    """Read the front-end’s assertion from Firestore and seed Python’s MSAL cache."""
    doc = db.collection("users").document(uid) \
             .collection("msal").document("assertion").get()
    if not doc.exists:
        print(f"[WARN] No MSAL assertion for user {uid}")
        return False

    user_assertion = doc.to_dict().get("assertion")
    # optional: delete it so you only do this once
    db.collection("users").document(uid) \
      .collection("msal").document("assertion").delete()

    # On-Behalf-Of exchange
    result = msal_app.acquire_token_on_behalf_of(
        user_assertion=user_assertion,
        scopes=GRAPH_SCOPES
    )
    if "access_token" not in result:
        print("❌ OBO failed:", result.get("error_description"))
        return False

    print("✅ OBO succeeded for user", uid)
    # msal_app.token_cache now holds a refresh token for silent renew
    return True
