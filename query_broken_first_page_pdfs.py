# smart_pdf_question.py

import os
import tempfile
import fitz
from openai import OpenAI, OpenAIError

client = OpenAI()       # expects OPENAI_API_KEY in your env
QUESTION = "What is the minimum ceiling height in the warehouse?"

# 1️⃣ Cache all existing user_data uploads by filename
existing_files = {
    f.filename: f.id
    for f in client.files.list(purpose="user_data").data
}

def get_or_upload(pdf_path):
    """Return an existing file_id if present, otherwise upload and return new file_id."""
    name = os.path.basename(pdf_path)
    if name in existing_files:
        print(f"✅ Reusing existing upload for {name!r}")
        return existing_files[name]

    print(f"⬆️  Uploading {name!r}…")
    upload = client.files.create(file=open(pdf_path, "rb"), purpose="user_data")
    file_id = upload.id
    existing_files[name] = file_id
    print(f"   → New file_id: {file_id}")
    return file_id

def ask_via_responses(file_id):
    """Upload + ask the Responses API, with a 30s timeout."""
    return client.responses.create(
        model="gpt-4.1",
        input=[{
            "role": "user",
            "content": [
                {"type": "input_file", "file_id": file_id},
                {"type": "input_text", "text": QUESTION},
            ]
        }],
        timeout=30
    ).output_text.strip()

def drop_first_page(pdf_path):
    """Write a new temp PDF containing pages 2..end, return its path."""
    doc = fitz.open(pdf_path)
    out = fitz.open()
    for p in range(1, doc.page_count):
        out.insert_pdf(doc, from_page=p, to_page=p)
    tmp_path = tempfile.mktemp(suffix=".pdf")
    out.save(tmp_path)
    out.close()
    return tmp_path

def smart_query(pdf_path):
    name = os.path.basename(pdf_path)
    print(f"\n📄 Processing {name!r}")

    # 2️⃣ Get or upload the original
    fid = get_or_upload(pdf_path)

    # 3️⃣ Try the full-PDF question
    try:
        ans = ask_via_responses(fid)
        print("✅ Full-PDF answer:", ans)
        return
    except OpenAIError as e:
        print(f"⚠️  Full-PDF failed or timed-out: {e}")

    # 4️⃣ Fallback: drop page 1, upload that new PDF, and retry
    print("➡️  Falling back to pages 2+ only…")
    short_pdf = drop_first_page(pdf_path)
    short_fid = get_or_upload(short_pdf)  # this will upload temp PDF
    try:
        ans2 = ask_via_responses(short_fid)
        print("✅ Page-1-skipped answer:", ans2)
    except OpenAIError as e2:
        print(f"❌ Still failed without page 1: {e2}")

if __name__ == "__main__":
    # 5️⃣ Your sealed-building PDFs
    pdfs = [
        "Sealed Bldg C 10-24-23.pdf",
        "Sealed Bldg D 10-24-23.pdf",
    ]
    for pdf in pdfs:
        if os.path.exists(pdf):
            smart_query(pdf)
        else:
            print(f"⚠️  File not found: {pdf!r}")
