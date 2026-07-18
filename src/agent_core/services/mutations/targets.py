"""Pure write-set calculation for immutable mutation plans."""

from ..beancount import _cfg
from ..ledger_paths import sidecar_target_file
from ..types import LedgerConfig
from .plans import MutationPlan


def potential_write_targets(
    plan: MutationPlan, config: LedgerConfig | None = None
) -> tuple[str, ...]:
    """Return every path replay may create or modify, in stable order."""
    resolved = _cfg(config)
    targets: list[str] = []
    for operation in plan.operations:
        if operation.kind in {"append", "open", "close", "price"}:
            targets.extend(
                (resolved.sidecar_main_path, sidecar_target_file(resolved))
            )
        elif operation.kind in {"replace", "delete"} and operation.target_file:
            targets.append(operation.target_file)
    return tuple(dict.fromkeys(targets))


def sealed_write_set_matches(
    plan: MutationPlan, config: LedgerConfig | None = None
) -> bool:
    """Require the approved preconditions to cover the exact replay write set.

    Append and open operations derive their sidecar month file from the active
    date and layout.  Comparing that derivation with the sealed preconditions
    makes a month rollover or request-layout change fail closed before replay.
    """
    return tuple(condition.path for condition in plan.preconditions) == (
        potential_write_targets(plan, config)
    )
