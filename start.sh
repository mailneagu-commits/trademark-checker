#!/bin/bash
cd "$(dirname "$0")/backend"
echo "Pornesc serverul Trademark Checker..."
echo "Deschide http://localhost:8000 in browser"
python3 -m uvicorn main:app --reload --port 8000
