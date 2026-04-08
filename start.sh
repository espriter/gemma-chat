#!/bin/bash
cd /srv/gemma-chat
/srv/gemma-chat/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8095 --app-dir /srv/gemma-chat
