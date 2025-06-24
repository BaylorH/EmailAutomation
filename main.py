# main.py
from mailer import send_weekly_email

def main():
    # list your users however you do
    user_ids = ["6h0p7yYDnSZOd5CAy6qCvs4zw4D2"]
    for uid in user_ids:
        # fetch client emails from Firestore as you already do...
        recipients = ["bp21harrison#gmail.com"]
        send_weekly_email(uid, recipients)

if __name__ == "__main__":
    main()
