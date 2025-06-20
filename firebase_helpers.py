import requests

FIREBASE_BUCKET = "email-automation-cache.appspot.com"

def download_token(api_key: str, output_file="msal_token_cache.bin", user_id="default_user"):
    url = (
        f"https://firebasestorage.googleapis.com/v0/b/"
        f"{FIREBASE_BUCKET}/o/msal_caches%2F{user_id}%2Fmsal_token_cache.bin"
        f"?alt=media&key={api_key}"
    )
    r = requests.get(url)
    if r.status_code == 200:
        with open(output_file, "wb") as f:
            f.write(r.content)
        print(f"✅ Token cache downloaded for {user_id}.")
    else:
        print(f"❌ Download failed for {user_id} ({r.status_code}):", r.text)

def upload_token(api_key: str, input_file="msal_token_cache.bin", user_id="default_user"):
    with open(input_file, "rb") as f:
        data = f.read()
    
    object_path = f"msal_caches/{user_id}/msal_token_cache.bin"
    url = (
        f"https://firebasestorage.googleapis.com/v0/b/"
        f"{FIREBASE_BUCKET}/o/msal_caches%2F{user_id}%2Fmsal_token_cache.bin"
        f"?uploadType=media&name=msal_caches/{user_id}/msal_token_cache.bin&key={api_key}"
    )
    
    headers = {"Content-Type": "application/octet-stream"}
    r = requests.post(url, headers=headers, data=data)
    
    if r.status_code in [200, 201]:
        print(f"✅ Token cache uploaded for {user_id}.")
    else:
        print(f"❌ Upload failed for {user_id} ({r.status_code}):", r.text)

