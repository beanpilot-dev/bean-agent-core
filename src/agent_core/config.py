"""Agent-core runtime configuration.

All config is read from environment variables. No file-based config.
"""

import logging
import os

logger = logging.getLogger(__name__)

_raw = os.environ.get("WORKSPACE_TTL_SECONDS", "900")
try:
    WORKSPACE_TTL_SECONDS = int(_raw)
except ValueError:
    logger.warning(
        "WORKSPACE_TTL_SECONDS=%r is not an integer, falling back to 900", _raw,
    )
    WORKSPACE_TTL_SECONDS = 900
"""TTL in seconds for cached workspace clones in /tmp/bean_cache/.

- 900 (default): SaaS, 15 min — short TTL to limit disk exposure
- 86400: self-hosted, 24h — long TTL for better UX
- 0: always fresh clone, no caching
- -1: infinite TTL, caches never expire (self-hosted only)
"""
