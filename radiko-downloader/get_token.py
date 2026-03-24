# /// script
# dependencies = [
#   "google-api-python-client",
#   "google-auth-httplib2",
#   "google-auth-oauthlib"
# ]
# ///

import os
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# The exact same minimal scope we used before
SCOPES = ['https://www.googleapis.com/auth/drive.file']

def main():
    flow = InstalledAppFlow.from_client_secrets_file('client_secret.json', SCOPES)
    # This opens a web browser for you to log in
# Force it to use port 8080 and prevent it from opening a CLI browser
    creds = flow.run_local_server(port=8080, open_browser=False)
    
    # Save the credentials for the next run
    with open('token.json', 'w') as token:
        token.write(creds.to_json())
    os.chmod('token.json', 0o600)
    print("Success! token.json has been created.")
    print("IMPORTANT: Delete client_secret.json — it is not needed at runtime.")
    print("  rm client_secret.json")

if __name__ == '__main__':
    main()
