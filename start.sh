#!/bin/bash
# Start the 小红书调研助手 chat GUI (default http://localhost:8766, override with XHS_PORT).
cd "$(dirname "$0")"
PORT="${XHS_PORT:-8766}"
(sleep 1.5 && .venv/bin/python -m webbrowser "http://localhost:$PORT" >/dev/null) &
exec .venv/bin/python -m xhs_reader.server
