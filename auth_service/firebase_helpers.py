import requests

FIREBASE_BUCKET = "email-automation-cache.firebasestorage.app"

def download_token(api_key: str, output_file="msal_token_cache.bin", user_id="default_user"):
    object_path = f"msal_caches/{user_id}/msal_token_cache.bin"
    url = (
        f"https://firebasestorage.googleapis.com/v0/b/{FIREBASE_BUCKET}/o/"
        f"{object_path.replace('/', '%2F')}?alt=media&key={api_key}"
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
        f"https://firebasestorage.googleapis.com/v0/b/{FIREBASE_BUCKET}/o?"
        f"uploadType=media&name={object_path}&key={api_key}"
    )
    
    headers = {"Content-Type": "application/octet-stream"}
    r = requests.post(url, headers=headers, data=data)
    
    if r.status_code in [200, 201]:
        print(f"✅ Token cache uploaded for {user_id}.")
    else:
        print(f"❌ Upload failed for {user_id} ({r.status_code}):", r.text)

def upload_excel(api_key: str, input_file="responses.xlsx", user_id="default_user"):
    with open(input_file, "rb") as f:
        data = f.read()
    
    object_path = f"excels/{user_id}/responses.xlsx"
    url = (
        f"https://firebasestorage.googleapis.com/v0/b/{FIREBASE_BUCKET}/o?"
        f"uploadType=media&name={object_path}&key={api_key}"
    )

    headers = {
        "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    }
    
    r = requests.post(url, headers=headers, data=data)
    
    if r.status_code in [200, 201]:
        print(f"✅ Excel file uploaded for {user_id}.")
    else:
        print(f"❌ Excel upload failed for {user_id} ({r.status_code}):", r.text)
