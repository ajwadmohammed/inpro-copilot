#!/usr/bin/env bash
PYTHONPATH=src python -m uvicorn inpro_copilot.api:app --port 8000
