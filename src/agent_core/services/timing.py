"""Payload-free timing for fixed preparation and publication stages."""

import logging
import time
from contextlib import contextmanager

from ..context import agent_request_id

logger = logging.getLogger(__name__)


@contextmanager
def stage_timer(stage: str):
    started = time.monotonic()
    try:
        yield
    finally:
        logger.info(
            "operation_stage completed stage=%s duration_ms=%s request_id=%s",
            stage,
            int((time.monotonic() - started) * 1000),
            agent_request_id.get(),
        )
