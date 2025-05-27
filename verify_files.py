# verify_files.py

import os
from openai import OpenAI

# 1️⃣ Initialize client (assumes OPENAI_API_KEY in your env)
client = OpenAI()

# 2️⃣ Your existing file IDs
file_ids = [
    "file-P3ZwbUqEZSHi97tXWBvjDY",
    "file-HnPKpAHWzmkHvdQwCs7WK9",
    "file-TmD66sL7PHnuMChuy1FYP8",
]

# 3️⃣ Loop through each, fetch metadata & ask a question
for fid in file_ids:
    try:
        # Fetch metadata
        info = client.files.retrieve(fid)
        size_kb = info.bytes / 1024
        print(f"\n📄 {info.filename!r}: {size_kb:.1f} KB, purpose={info.purpose}")

        # Ask a simple question of page 1
        resp = client.responses.create(
            model="gpt-4o-mini",   # vision-capable
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_file", "file_id": fid},
                        {"type": "input_text", "text": "What is the property address on page 1?"}
                    ]
                }
            ]
        )
        print("🔍 Address:", resp.output_text.strip())

    except Exception as e:
        print(f"❌ Error with {fid}: {e}")
