"""
Omnibridge: Autonomous In-Transit Cross-Chain Execution System
"""

__version__ = "0.1.0"

# A .env is loaded once, here, because module-level configuration is read during import
# (graph.MAX_DISCOVERY_RETRIES, discovery_agent.MODEL) and would otherwise miss it.
# A real environment variable always wins; OMNIBRIDGE_NO_DOTENV=1 skips the file.
from omnibridge.config import load_env as _load_env  # noqa: E402

_load_env()
