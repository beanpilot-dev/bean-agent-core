from beanbench.requestor import (
    extract_beancount_block,
    extract_pending_action_preview,
)

TRANSACTION = '''2026-06-23 * "Trader Joe's" "groceries"
  Expenses:Food:Groceries          75.50 USD
  Liabilities:CreditCard:Primary  -75.50 USD'''


def test_pending_action_display_diff_is_a_preview_fallback() -> None:
    actions = [{"display": {"kind": "transaction_preview", "diff": TRANSACTION}}]

    assert extract_pending_action_preview(actions) == TRANSACTION


def test_pending_action_canonical_preview_is_preferred() -> None:
    actions = [{"display": {"canonical_preview": "canonical", "diff": "fallback"}}]

    assert extract_pending_action_preview(actions) == "canonical"


def test_markdown_preview_remains_available_as_primary_path() -> None:
    text = f"Prepared action:\n```beancount\n{TRANSACTION}\n```"

    assert extract_beancount_block(text) == TRANSACTION
