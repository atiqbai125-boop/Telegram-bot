#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example. Add your BOT_TOKEN and a private DASHBOARD_SECRET, then run this script again."
  exit 1
fi
if grep -q 'put_your_bot_token_here' .env || grep -q 'choose_a_long_unique_password' .env; then
  echo "Edit .env first: replace the template BOT_TOKEN and DASHBOARD_SECRET values."
  exit 1
fi
python3 -m pip install -r requirements.txt
exec python3 run.py
