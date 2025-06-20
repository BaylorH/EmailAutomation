import os
from firebase_helpers import download_token, upload_token

FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY")
USER_ID = "test_user"  # You can use any string here for testing

# Temporarily override the USER_ID in firebase_helpers (or modify it to accept an arg)
# If you want it dynamic, refactor firebase_helpers to accept USER_ID as a param

print("Uploading...")
upload_token(FIREBASE_API_KEY, input_file="test_token.bin")

print("Downloading...")
download_token(FIREBASE_API_KEY, output_file="downloaded_test_token.bin")
