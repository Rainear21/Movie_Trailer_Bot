#!/usr/bin/env bash
# Linux/macOS launcher: sets up a virtual environment on first run, then starts the bot.
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
fi

if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env - fill in your Discord details (e.g. nano .env), then run this again. See README."
    exit 1
fi

exec .venv/bin/python bot.py
