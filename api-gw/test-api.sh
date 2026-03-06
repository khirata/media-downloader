#!/bin/bash

# Configuration
# Replace these with the outputs from `terraform apply`
API_ENDPOINT="https://8rs91vcko2.execute-api.us-west-2.amazonaws.com/prod/publish"
API_KEY="l7RvZePScA8DO6efwxp4D3MQvEyDqNxgyPznO9Qb"
SECRET_TOKEN="9W9pcd*10CM&&6&dJE83m"
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
