from openai import OpenAI

client = OpenAI()

asst = client.beta.assistants.create(
    name="Jill PDF Extractor",
    model="gpt-4o",   # or any vision+code model
    instructions=(
        "You have three PDF files (file-IDs provided). "
        "When asked, you should read them—using the code_interpreter tool—to "
        "locate and extract fields such as Property Address, City, Total SF, etc. "
        "You always respond with exactly the data requested, in CSV format "
        "or as a JSON object, as instructed."
    ),
    tools=[{"type": "code_interpreter"}],
    tool_resources={
        "code_interpreter": {
            "file_ids": [
                "file-P3ZwbUqEZSHi97tXWBvjDY",  # Brochure
                "file-HnPKpAHWzmkHvdQwCs7WK9",  # Bldg C
                "file-TmD66sL7PHnuMChuy1FYP8",  # Bldg D
            ]
        }
    },
    response_format="auto",  # lets you get back JSON if you set JSON mode in instructions
)
print("Assistant ID:", asst.id)
