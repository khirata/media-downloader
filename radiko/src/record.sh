#!/bin/bash

# Ensure we have at least a Station ID and one Start Time
if [ "$#" -lt 2 ]; then
    echo "Usage: ./record.sh <STATION_ID> <START_TIME_1> [<START_TIME_2> ...]"
    echo ""
    echo "Single Segment Example:"
    echo "  ./record.sh FMJ 202602230200"
    echo ""
    echo "Multi-Segment Example (Stitches them together):"
    echo "  ./record.sh FMJ 202602230200 202602230300 202602230400"
    exit 1
fi

# Pass all arguments exactly as typed to the Python worker
python3 /app/src/worker.py "$@"
