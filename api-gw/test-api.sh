#!/bin/bash

# Configuration
cd "$(dirname "$0")"

API_ENDPOINT=$(terraform output -raw api_endpoint)
API_KEY=$(terraform output -raw api_key)

# Radiko test payload
PAYLOAD='{
  "urls": [
    "https://radiko.jp/#!/ts/FMJ/20260301130000",
    "https://radiko.jp/#!/ts/FMJ/20260301140000",
    "https://radiko.jp/#!/ts/FMJ/20260301150000",
    "https://radiko.jp/#!/ts/FMJ/20260301160000"
  ]
}'

echo "Testing API Gateway endpoint: $API_ENDPOINT"

curl -X POST "$API_ENDPOINT" \
  -H "Content-Type: application/json" \
  -H "x-api-key: $API_KEY" \
  -H "x-api-secret: $SECRET_TOKEN" \
  -d "$PAYLOAD"

echo -e "\nDone."
