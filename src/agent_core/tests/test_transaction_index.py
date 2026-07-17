"""Contract tests for transaction references, boundaries, and detail lookup."""

from pathlib import Path

from agent_core.services.queries import LedgerQueryService
from agent_core.services.transaction_index import (
    TransactionIndex,
    parse_transaction_ref,
)


def _add_transaction(workspace: Path, text: str, filename: str = "2026-07.beancount") -> None:
    path = workspace / "data" / "agent_inc" / filename
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n" + text)


def test_search_and_detail_use_exact_parser_directive_and_fingerprint(
    ledger_workspace: Path,
) -> None:
    directive = (
        '2026-07-01 * "餐厅" "午餐" #meal ^receipt\n'
        '  merchant: "店铺"\n'
        "  ; a comment inside the directive\n"
        "  Expenses:Food:Dining  25 CNY\n"
        "  Assets:Cash          -25 CNY\n"
    )
    _add_transaction(ledger_workspace, directive)

    found = LedgerQueryService.find_transactions(
        str(ledger_workspace), narration_contains="午餐"
    )
    assert found.count == 1
    summary = found.rows[0]
    assert set(summary) == {"transaction_ref", "date", "payee", "narration", "postings"}
    assert summary["postings"] == [
        {"account": "Expenses:Food:Dining", "amount": "25 CNY"},
        {"account": "Assets:Cash", "amount": "-25 CNY"},
    ]

    detail = LedgerQueryService.get_transaction(str(ledger_workspace), summary["transaction_ref"])
    assert detail.status == "SUCCESS"
    assert detail.transaction is not None
    assert detail.transaction["directive"] == directive
    assert detail.transaction["source_path"] == "data/agent_inc/2026-07.beancount"
    assert detail.transaction["source_start_line"] > 0
    assert detail.transaction["source_end_line"] == detail.transaction["source_start_line"] + 4
    assert detail.transaction["metadata"] == {"merchant": "店铺"}
    assert detail.transaction["tags"] == ["meal"]
    assert detail.transaction["links"] == ["receipt"]
    assert detail.revision_fingerprint.startswith("sha256:")
    assert "Test Ledger" not in detail.directive
    assert "file_content" not in detail.transaction


def test_identical_directives_get_distinct_references_and_concise_rows(
    ledger_workspace: Path,
) -> None:
    duplicate = (
        '2026-08-01 * "Same"\n'
        "  Expenses:Food:Dining  10 CNY\n"
        "  Assets:Cash          -10 CNY\n"
    )
    _add_transaction(ledger_workspace, duplicate, "first.beancount")
    _add_transaction(ledger_workspace, duplicate, "second.beancount")
    sidecar_main = ledger_workspace / "data" / "agent_inc" / "main.beancount"
    sidecar_main.write_text(
        sidecar_main.read_text()
        + '\ninclude "first.beancount"\ninclude "second.beancount"\n',
        encoding="utf-8",
    )

    result = LedgerQueryService.find_transactions(str(ledger_workspace), narration_contains="Same")
    assert result.count == result.total == 2
    assert len({row["transaction_ref"] for row in result.rows}) == 2
    assert all("directive" not in row for row in result.rows)

    details = [
        LedgerQueryService.get_transaction(str(ledger_workspace), row["transaction_ref"])
        for row in result.rows
    ]
    assert all(detail.status == "SUCCESS" for detail in details)
    assert details[0].revision_fingerprint == details[1].revision_fingerprint


def test_reference_is_stable_for_formatting_but_fails_closed_when_moved(
    ledger_workspace: Path,
) -> None:
    found = LedgerQueryService.find_transactions(str(ledger_workspace), narration_contains="Lunch")
    reference = found.rows[0]["transaction_ref"]
    before = LedgerQueryService.get_transaction(str(ledger_workspace), reference)
    assert before.status == "SUCCESS"

    source = ledger_workspace / "data" / "agent_inc" / "2026-07.beancount"
    source.write_text(
        source.read_text().replace("Dining  85", "Dining    85"), encoding="utf-8"
    )
    after_format = LedgerQueryService.get_transaction(str(ledger_workspace), reference)
    assert after_format.status == "SUCCESS"
    assert after_format.revision_fingerprint != before.revision_fingerprint

    source.write_text("; moved line\n" + source.read_text(), encoding="utf-8")
    moved = LedgerQueryService.get_transaction(str(ledger_workspace), reference)
    assert moved.status == "ERROR"
    assert moved.error_code == "STALE_TRANSACTION_REF"


def test_reference_parser_and_missing_errors_are_deterministic(ledger_workspace: Path) -> None:
    found = LedgerQueryService.find_transactions(str(ledger_workspace), narration_contains="Lunch")
    reference = found.rows[0]["transaction_ref"]
    payload = parse_transaction_ref(reference)
    assert payload is not None
    assert payload["version"] == 1
    assert payload["path"] == "data/agent_inc/2026-07.beancount"

    malformed = LedgerQueryService.get_transaction(str(ledger_workspace), "txn_v1_not-a-ref")
    missing = LedgerQueryService.get_transaction(
        str(ledger_workspace), reference[:-1] + ("0" if reference[-1] != "0" else "1")
    )
    assert malformed.error_code == "MALFORMED_TRANSACTION_REF"
    assert missing.error_code == "MALFORMED_TRANSACTION_REF"


def test_non_sidecar_included_transactions_are_read_without_file_leakage(
    ledger_workspace: Path,
) -> None:
    legacy = ledger_workspace / "data" / "legacy.beancount"
    legacy.write_text(
        '2026-09-01 * "Legacy"\n'
        "  Expenses:Food:Dining  3 CNY\n"
        "  Assets:Cash          -3 CNY\n"
        "\n; unrelated source text\n",
        encoding="utf-8",
    )
    entry = ledger_workspace / "data" / "main.beancount"
    entry.write_text(entry.read_text() + 'include "legacy.beancount"\n', encoding="utf-8")

    result = LedgerQueryService.find_transactions(
        str(ledger_workspace), narration_contains="Legacy"
    )
    detail = LedgerQueryService.get_transaction(
        str(ledger_workspace), result.rows[0]["transaction_ref"]
    )
    assert detail.status == "SUCCESS"
    assert detail.source_path == "data/legacy.beancount"
    assert "unrelated source text" not in detail.directive


def test_index_does_not_retain_whole_source_files(ledger_workspace: Path) -> None:
    index = TransactionIndex.build(str(ledger_workspace))
    assert index.transactions
    assert all(not hasattr(transaction, "file_content") for transaction in index.transactions)
