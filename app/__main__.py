"""
Entry point for running the LiveKit agent worker as a module.

Usage:
    python -m app.livekit_agent dev        # Development mode (auto-reload)
    python -m app.livekit_agent start      # Production mode
"""

from app.livekit_agent import entrypoint, prewarm

from livekit.agents import cli, WorkerOptions

if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
        )
    )
