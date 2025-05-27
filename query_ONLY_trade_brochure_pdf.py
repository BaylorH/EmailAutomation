# assisant.py

from openai import OpenAI

client = OpenAI()  # make sure OPENAI_API_KEY is set in your env

# Upload your PDF
file = client.files.create(
    file=open("135 Trade Center Court - Brochure.pdf", "rb"),
    purpose="user_data"
)

# Ask a simple question about it
response = client.responses.create(
    model="gpt-4.1",
    input=[
        {
            "role": "user",
            "content": [
                {
                    "type": "input_file",
                    "file_id": file.id,
                },
                {
                    "type": "input_text",
                    "text": "What is the Total SF?",
                },
            ]
        }
    ]
)

print(response.output_text)
