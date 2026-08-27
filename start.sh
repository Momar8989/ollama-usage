#!/bin/bash
# Launch the Ollama usage collector. Used by crontab @reboot.
cd "$(dirname "$0")"
export OLLAMA_API_KEY="$(grep '^OLLAMA_API_KEY=' ~/.hermes/.env | cut -d= -f2)"
exec python3 app.py >> service.log 2>&1