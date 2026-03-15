#!/usr/bin/env python3
import argparse
import sys
import os
import json
import urllib.request
from urllib.error import URLError, HTTPError
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

def load_env():
    # Load .env file from the current directory
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    if os.path.exists(env_path):
        with open(env_path, 'r') as f:
            for line in f:
                if line.strip() and not line.startswith('#'):
                    key, val = line.strip().split('=', 1)
                    os.environ[key] = val.strip(' "\'')



def main():
    parser = argparse.ArgumentParser(description="Trigger API Gateway for scheduled Radiko recordings.")
    parser.add_argument("--station", required=True, help="Radiko Station ID (e.g., FMJ)")
    parser.add_argument("--desc", help="Description or name of the show")
    parser.add_argument("start_times", nargs="+", help="Start times in full format (e.g., 20260314130000)")
    
    args = parser.parse_args()
    load_env()

    endpoint = os.environ.get('MEDIA_RECORDER_API_ENDPOINT')
    api_key = os.environ.get('MEDIA_RECORDER_API_KEY')

    if not endpoint or not api_key:
        print("Error: MEDIA_RECORDER_API_ENDPOINT or MEDIA_RECORDER_API_KEY not set in .env")
        sys.exit(1)

    urls = []
    for st in args.start_times:
        urls.append(f"https://radiko.jp/#!/ts/{args.station}/{st}")

    payload = {
        "urls": urls
    }
    if args.desc:
        payload["description"] = args.desc

    payload_bytes = json.dumps(payload).encode('utf-8')

    print(f"Sending payload to {endpoint} :")
    print(json.dumps(payload, indent=2))

    req = urllib.request.Request(str(endpoint), data=payload_bytes, method='POST', headers={
        'Content-Type': 'application/json',
        'x-api-key': str(api_key)
    })

    try:
        with urllib.request.urlopen(req) as response:
            print(f"Success! HTTP Status: {response.status}")
    except HTTPError as e:
        print(f"API Error: HTTP {e.code} - {e.reason}")
        sys.exit(1)
    except URLError as e:
        print(f"Network Error: Failed to reach API - {e.reason}")
        sys.exit(1)

if __name__ == "__main__":
    main()
