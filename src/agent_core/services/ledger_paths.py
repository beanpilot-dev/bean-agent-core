"""Pure ledger path calculations shared by read and mutation services."""

from datetime import date
from pathlib import PurePosixPath

from .beancount import _cfg
from .types import LedgerConfig


def sidecar_target_file(ledger_config: LedgerConfig | None = None) -> str:
    config = _cfg(ledger_config)
    return f"{config.sidecar_write_dir}/{date.today():%Y-%m}.beancount"


def is_sidecar_path(
    relative_path: str, ledger_config: LedgerConfig | None = None
) -> bool:
    """Return whether a normalized repository path is inside the sidecar."""
    config = _cfg(ledger_config)
    path = PurePosixPath(relative_path)
    write_dir = PurePosixPath(config.sidecar_write_dir)
    return (
        bool(relative_path)
        and not path.is_absolute()
        and ".." not in path.parts
        and path != write_dir
        and write_dir in path.parents
    )
