import requests

# Constants (customize as needed)
FIREBASE_BUCKET = "email-automation-cache.appspot.com"
USER_ID = "user123"  # change this dynamically per user in the future

def download_token(api_key: str, output_file="msal_token_cache.bin"):
    url = f"https://firebasestorage.googleapis.com/v0/b/{FIREBASE_BUCKET}/o/msal_caches%2F{USER_ID}%2Fmsal_token_cache.bin?alt=media&key={api_key}"
    r = requests.get(url)
    if r.status_code == 200:
        with open(output_file, "wb") as f:
            f.write(r.content)
        print("✅ Token cache downloaded.")
    else:
        print(f"❌ Download failed ({r.status_code}):", r.text)

def upload_token(api_key: str, input_file="msal_token_cache.bin"):
    with open(input_file, "rb") as f:
        data = f.read()
    url = f"https://firebasestorage.googleapis.com/v0/b/{FIREBASE_BUCKET}/o/msal_caches%2F{USER_ID}%2Fmsal_token_cache.bin?uploadType=media&name=msal_caches/{USER_ID}/msal_token_cache.bin&key={api_key}"
    headers = {"Content-Type": "application/octet-stream"}
    r = requests.post(url, headers=headers, data=data)
    if r.status_code in [200, 201]:
        print("✅ Token cache uploaded.")
    else:
        print(f"❌ Upload failed ({r.status_code}):", r.text)
