# pdf_question.py

import os
from openai import OpenAI

client = OpenAI()  # expects OPENAI_API_KEY in your env

PDF_NAME = "135 Trade Center Court - Brochure.pdf"
QUESTION = "What is the property address on page 1?"

# 1️⃣ Fetch all user_data files once
files = client.files.list(purpose="user_data").data

# 2️⃣ See if our PDF is already uploaded
existing = next((f for f in files if f.filename == PDF_NAME), None)

if existing:
    file_id = existing.id
    print(f"✅ Found existing upload: {PDF_NAME!r} → {file_id}")
else:
    # 3️⃣ Not found → upload it now
    upload = client.files.create(
        file=open(PDF_NAME, "rb"),
        purpose="user_data"
    )
    file_id = upload.id
    print(f"✅ Uploaded new file: {PDF_NAME!r} → {file_id}")

# 4️⃣ Ask the model about that file
response = client.responses.create(
    model="gpt-4.1",
    input=[
        {
            "role": "user",
            "content": [
                {"type": "input_file", "file_id": file_id},
                {"type": "input_text", "text": QUESTION},
            ]
        }
    ]
)

# 5️⃣ Print the answer
print("\n🗒️  Response:")
print(response.output_text)
