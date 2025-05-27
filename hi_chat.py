# test.py

from openai import OpenAI

client = OpenAI()

response = client.responses.create(
    model="gpt-4.1",
    input=[
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": "hi"
                }
            ]
        }
    ],
    text={ "format": { "type": "text" } },
    reasoning={},
    tools=[],
    temperature=1,
    max_output_tokens=2048,
    top_p=1,
    store=True
)

# ← add this to see what you got back:
print(response.output_text)
