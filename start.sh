#!/bin/bash
# Start the 小红书调研助手 chat GUI from a source checkout (opens the browser).
cd "$(dirname "$0")"
exec .venv/bin/python -m xhs_reader "$@"
