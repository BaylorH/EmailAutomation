# upload_files.py

import os
from openai import OpenAI

# 1️⃣ Initialize client (assumes OPENAI_API_KEY is in your env)
client = OpenAI()

# 2️⃣ List your PDF filenames (must be in the same folder)
pdf_paths = [
    "135 Trade Center Court - Brochure.pdf",
    "Sealed Bldg C 10-24-23.pdf",
    "Sealed Bldg D 10-24-23.pdf",
]

# 3️⃣ Upload each and print the resulting file_id
for path in pdf_paths:
    if not os.path.exists(path):
        print(f"⚠️  File not found: {path}")
        continue

    upload = client.files.create(
        file=open(path, "rb"),
        purpose="user_data"
    )
    print(f"✅ Uploaded {path!r} → file_id: {upload.id}")

# 4️⃣ Done — script will exit here
