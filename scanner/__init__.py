"""TradePilot — directional scanner + broker integration package."""

from __future__ import annotations

from dotenv import find_dotenv, load_dotenv

# Load .env at the repo root so any entry point that imports `scanner.*`
# (api server, CLI scripts, tests) sees POLYGON_API_KEY, IB_BROKER_PORT, etc.
# without needing to export them in the shell. Real shell env wins over .env.
load_dotenv(find_dotenv(usecwd=True), override=False)

__all__ = ["__version__"]

__version__ = "0.1.0"
