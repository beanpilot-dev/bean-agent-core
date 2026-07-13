"""Pure ledger path calculations shared by read and mutation services."""

from datetime import date

from .beancount import _cfg
from .types import LedgerConfig


def sidecar_target_file(ledger_config: LedgerConfig | None = None) -> str:
    config = _cfg(ledger_config)
    return f"{config.sidecar_write_dir}/{date.today():%Y-%m}.beancount"
