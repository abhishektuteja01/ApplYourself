"""Keeps `uv run python -m src.discovery` working (LaunchAgent entry point)."""
import sys

from src.discovery.orchestrator import main

if __name__ == "__main__":
    sys.exit(main())
