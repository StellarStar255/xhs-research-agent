#!/bin/bash
# Start the 种草调研助手 chat GUI from a source checkout (opens the browser).
cd "$(dirname "$0")"
exec .venv/bin/python -m xhs_reader "$@"
