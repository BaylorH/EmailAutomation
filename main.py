from fastapi import FastAPI, HTTPException
from scheduler_runner_api import send_weekly_email, process_replies

app = FastAPI()

@app.get("/")
def root():
    return {"message": "API is up and running!"}

@app.post("/send")
def send():
    try:
        recipients = ["bp21harrison@gmail.com"]
        send_weekly_email(recipients)
        return {"status": "Emails sent"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/check")
def check():
    try:
        process_replies()
        return {"status": "Replies processed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
