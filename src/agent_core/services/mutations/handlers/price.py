"""Approval-gated preparation of one native Beancount price directive."""

from typing import Any

from ...ledger_paths import sidecar_target_file
from ...price_directives import (
    normalize_effective_at,
    parse_price_identity,
    price_fact_subject,
    price_state,
    validate_source,
)
from ...types import InvariantViolation, LedgerConfig
from ..facts import capture_price_state_fact
from ..planners import MutationPlanner
from .contracts import PreparedMutation


class PricePreparationHandler:
    handler_key = "price"

    def build(
        self,
        workspace: str,
        ledger_config: LedgerConfig | None = None,
        **kwargs: Any,
    ) -> PreparedMutation | InvariantViolation:
        price_date = str(kwargs.get("price_date") or "")
        base = str(kwargs.get("base_commodity") or "")
        price = str(kwargs.get("price") or "")
        quote = str(kwargs.get("quote_commodity") or "")
        source = str(kwargs.get("source") or "")
        effective_at = str(kwargs.get("effective_at") or "")
        commit_message = str(kwargs.get("commit_message") or "")

        identity, error = parse_price_identity(price_date, base, price, quote)
        if error or identity is None:
            return _invalid("PRICE_INPUT_INVALID", error or "Invalid price input.")
        if source_error := validate_source(source):
            return _invalid("PRICE_INPUT_INVALID", source_error)
        effective_value, effective_error = normalize_effective_at(effective_at)
        if effective_error or effective_value is None:
            return _invalid("PRICE_INPUT_INVALID", effective_error or "Invalid effective_at.")

        state = price_state(workspace, identity, ledger_config)
        if state.startswith("exact:"):
            return _invalid(
                "PRICE_ALREADY_RECORDED",
                "An identical price is already recorded for this date and commodity pair.",
            )
        if state != "absent":
            return _invalid(
                "PRICE_CONFLICT",
                "A different price is already recorded for this date and commodity pair.",
            )

        directive = (
            f"; BeanPilot source: {source.strip()}\n"
            f"; BeanPilot effective_at: {effective_value}\n"
            f"{identity.price_date} price {identity.base_commodity} "
            f"{identity.value} {identity.quote_commodity}\n"
        )
        plan = MutationPlanner.price(directive, commit_message).with_semantic_facts(
            (capture_price_state_fact(workspace, identity, ledger_config),)
        )
        target = sidecar_target_file(ledger_config)
        return PreparedMutation(
            handler_key=self.handler_key,
            action_type="record_price",
            plan=plan,
            preview_fields={
                "price_date": identity.price_date,
                "base_commodity": identity.base_commodity,
                "price": identity.value,
                "quote_commodity": identity.quote_commodity,
                "source": source.strip(),
                "effective_at": effective_value,
                "directive": directive,
                "target_file": target,
                "classification": "record_price",
            },
            execution_spec={
                "price_date": identity.price_date,
                "base_commodity": identity.base_commodity,
                "price": identity.value,
                "quote_commodity": identity.quote_commodity,
                "source": source.strip(),
                "effective_at": effective_value,
                "commit_message": commit_message,
            },
            display_fields={
                "kind": "price_preview",
                "title": "Record price",
                "summary": "Record one reviewed price directive in the agent sidecar.",
                "directive": directive,
                "target_file": target,
                "price_date": identity.price_date,
                "base_commodity": identity.base_commodity,
                "price": identity.value,
                "quote_commodity": identity.quote_commodity,
                "source": source.strip(),
                "effective_at": effective_value,
                "classification": "record_price",
                "diff": f"--- /dev/null\n+++ {target}\n@@ -0,0 +1,3 @@\n"
                + "".join(f"+{line}\n" for line in directive.rstrip("\n").splitlines()),
            },
            validation_fields={
                "target_file": target,
                "price_identity": price_fact_subject(identity),
                "source": source.strip(),
            },
            message=(
                "Price directive passed isolated validation. Show the complete "
                "directive and request approval."
            ),
            preview_target_field="target_file",
        )


def _invalid(invariant: str, remediation: str) -> InvariantViolation:
    return InvariantViolation(
        invariant=invariant,
        severity="HARD",
        remediation=remediation,
    )
