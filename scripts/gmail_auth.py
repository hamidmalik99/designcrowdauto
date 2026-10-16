"""Run ONCE on your own computer to get a Gmail read-only refresh token.

1. Google Cloud Console > APIs & Services > enable "Gmail API".
2. Credentials > Create OAuth client ID > Desktop app. Download the JSON as client_secret.json.
3. pip install google-auth-oauthlib && python scripts/gmail_auth.py client_secret.json
4. Sign in with the inbox replies go to. Copy the printed values into GitHub secrets.
"""
import json
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "client_secret.json"
    flow = InstalledAppFlow.from_client_secrets_file(path, SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    client = json.load(open(path))["installed"]
    print("\nAdd these as GitHub Actions secrets:\n")
    print(f"GMAIL_CLIENT_ID={client['client_id']}")
    print(f"GMAIL_CLIENT_SECRET={client['client_secret']}")
    print(f"GMAIL_REFRESH_TOKEN={creds.refresh_token}")
