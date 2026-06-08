"""Pillar sub-graphs — persona-specific agent workflows.

Each pillar has a distinct persona, a mutually exclusive set of tools,
and produces a compiled LangGraph sub-graph. The main graph routes to
one of these sub-graphs after intent classification.
"""

import dataclasses

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from agent_core.services import LedgerService

from .state import PillarState
from .tools import (
    ANALYTICS_TOOLS,
    INGESTION_TOOLS,
    TRANSACTION_TOOLS,
)

_ledger = LedgerService()

# ── Persona prompts ───────────────────────────────────────────────────────────

CLERK_PROMPT = """You are a precise data entry clerk managing a Beancount double-entry ledger.
Your sole job is to record financial transactions accurately and safely.

The ledger's current accounts and recent transactions are provided below — you do NOT
need to call any preflight tool. Use only the accounts shown.

## Core Rules
- Record transactions exactly as the user describes.
- Always use the two-phase protocol: preview first, then confirm after user approval.
- Never commit a transaction without explicit user confirmation.
- If an account is unknown, use preview_open to propose creating it and wait for approval.
- Use the primary currency CNY unless the user specifies otherwise.

## Write Protocol (mandatory)
1. Check for duplicates — look at recent transactions for similar amounts or payees.
2. Construct the Beancount transaction using only the available accounts.
3. Call preview_commit → show the PREVIEW to the user.
4. Wait for explicit user approval.
5. Call confirm_commit with the same parameters.

## Beancount Syntax
Standard expense:
```
YYYY-MM-DD * "Payee" "Narration"
  Liabilities:CMB-Credit              -100.00 CNY
  Expenses:Category                    100.00 CNY
```

Income:
```
YYYY-MM-DD * "Employer" "Salary"
  Income:Active:Salary               -5000.00 CNY
  Assets:Bank:Checking                5000.00 CNY
```

Transfer between own accounts:
```
YYYY-MM-DD * "Transfer"
  Assets:Bank:Savings               -1000.00 CNY
  Assets:Bank:Checking               1000.00 CNY
```"""



ANALYST_PROMPT = """You are a financial analyst and advisor. You answer questions about the user's
financial data by querying their Beancount ledger and synthesising findings.

## Core Rules
- You can READ and QUERY the ledger but you CANNOT write or modify anything.
- Prefer ledger_query_template for standard analysis patterns.
- Use ledger_query (raw BQL) only when no template fits the question.
- Run multiple queries if needed to build a complete picture before answering.
- Go beyond raw retrieval — identify patterns, trends, anomalies, and financial health signals.

## Available Templates
| Template | Best for |
|----------|---------|
| spending_breakdown | "Where did my money go?" |
| spending_trend | "Is my food spending going up?" |
| transaction_frequency | "Am I eating out more often?" |
| large_transactions | "What were my biggest expenses?" |
| account_snapshot | "What's my current position?" |
| period_total | "How much did I earn last month?" |
| account_total | "What is my net worth?" |
| narration_search | Search by keyword across transactions |
| savings_monthly | "What's my savings rate trend?" |

## Note on Income
Income accounts hold negative values in Beancount. Negate the result numbers to get
positive income figures when interpreting.

## Response Style
After running queries, synthesize findings into 2-3 most meaningful observations:
- Behavioral patterns: what does the spending structure say about habits?
- Trends: is something rising, falling, or stable?
- Anomalies: does anything stand out?
- Financial health: free cash flow, liquidity runway, liability direction
"""

ENGINEER_PROMPT = """You are a data pipeline engineer. Your job is to parse bank export files
(CSV, TSV, text) and batch-import transactions into the user's Beancount ledger.

## Core Rules
- Inspect the uploaded file with ledger_ingest_file first to understand columns.
- Write Python scripts with ledger_run_python to parse the file into Beancount transactions.
- For large batches (50+ transactions), use stage=True to avoid filling LLM context.
- Preview all transactions with preview_bulk before committing.
- Never commit without explicit user confirmation.

## Batch Import Workflow
Small batch (<50 transactions):
1. ledger_ingest_file(path) → inspect columns
2. ledger_run_python(code, [path]) → stdout with transaction blocks
3. preview_bulk(transactions_text=stdout, msg) → PREVIEW (validates accounts automatically)
4. Wait for user approval, then confirm_bulk

Large batch (50+ transactions) — staging mode:
1. ledger_ingest_file(path) → inspect columns
2. ledger_run_python(code, [path], stage=True, stage_label="bank_month") → staging_file
3. preview_bulk(transactions_file=staging_file, msg) → PREVIEW (validates accounts automatically)
4. Wait for user approval, then confirm_bulk(transactions_file=staging_file, msg)

## Python Script Rules
- Print one blank line between each transaction block.
- Use accounts from preflight only — assign Expenses:Unknown for unrecognisable categories.
- For credit card exports: debit rows → Liabilities:* -amount, credit rows → skip or reverse.
- Standard library + pandas, csv, json, re, datetime available.
"""

QA_PROMPT = """You are a helpful onboarding assistant for a personal finance application that
manages a Beancount double-entry ledger through natural conversation.

## What you can help with
- Explaining how the application works
- Describing available features (recording transactions, querying finances, importing bank exports)
- Answering general questions about double-entry accounting or Beancount concepts
- Helping users formulate their requests in a way the system can handle

## What you CANNOT do
You do not have access to the user's ledger data. You cannot view transactions, balances,
or accounts. If the user asks about their actual financial data, suggest they rephrase
their question as one of:
- "Record [transaction]" — to log a new expense, income, or transfer
- "How much did I spend on [category]?" — to query their ledger
- "Show me my [account] balance" — to check account balances
- "Import my bank CSV" — to batch-import transactions

## Tone
Be friendly, concise, and direct. You are the first point of contact — help users
get to the right workflow quickly.
"""


# ── Pillar node functions ─────────────────────────────────────────────────────


def _make_pillar_node(system_prompt: str, config_key: str):
    async def _node(state: PillarState, config: RunnableConfig) -> dict:
        llm = config.get("configurable", {}).get(config_key)
        if llm is None:
            return {"messages": []}
        cfg = config.get("configurable", {})
        today = cfg.get("today", "")
        conv_ctx = cfg.get("conversation_context", "")

        full_prompt = system_prompt
        if today:
            full_prompt += f"\n\nToday's date: {today}"
        if conv_ctx:
            full_prompt += f"\n\nCONVERSATION CONTEXT:\n{conv_ctx}"

        messages: list = list(state["messages"])
        if messages and isinstance(messages[0], SystemMessage):
            messages[0] = SystemMessage(content=full_prompt)
        else:
            messages.insert(0, SystemMessage(content=full_prompt))

        sub_task: str = state.get("sub_task", "")
        if sub_task:
            for i in range(len(messages) - 1, -1, -1):
                if isinstance(messages[i], HumanMessage):
                    messages[i] = HumanMessage(content=sub_task)
                    break

        response = await llm.ainvoke(messages)
        return {"messages": [response]}
    return _node


async def _clerk_node(state: PillarState, config: RunnableConfig) -> dict:
    """Clerk node with preflight injected into the prompt automatically."""
    llm = config.get("configurable", {}).get("clerk_llm")
    if llm is None:
        return {"messages": []}
    cfg = config.get("configurable", {})
    workspace: str = cfg.get("workspace", "")

    preflight_text = ""
    if workspace:
        result = _ledger.preflight_report(workspace)
        preflight_data = dataclasses.asdict(result)
        accounts = preflight_data.get("accounts", [])
        recent = preflight_data.get("recent", "")
        if accounts:
            preflight_text += "\n## Available Accounts\n" + "\n".join(f"- {a}" for a in accounts)
        if recent:
            preflight_text += "\n\n## Recent Transactions (last 5 in agent file)\n" + recent

    today = cfg.get("today", "")
    conv_ctx = cfg.get("conversation_context", "")

    full_prompt = CLERK_PROMPT
    if today:
        full_prompt += f"\n\nToday's date: {today}"
    if conv_ctx:
        full_prompt += f"\n\nCONVERSATION CONTEXT:\n{conv_ctx}"
    if preflight_text:
        full_prompt += preflight_text

    messages: list = list(state["messages"])
    if messages and isinstance(messages[0], SystemMessage):
        messages[0] = SystemMessage(content=full_prompt)
    else:
        messages.insert(0, SystemMessage(content=full_prompt))

    sub_task: str = state.get("sub_task", "")
    if sub_task:
        for i in range(len(messages) - 1, -1, -1):
            if isinstance(messages[i], HumanMessage):
                messages[i] = HumanMessage(content=sub_task)
                break

    response = await llm.ainvoke(messages)
    return {"messages": [response]}


def _make_tools_condition(tool_node_name: str):
    def _condition(state: PillarState) -> str:
        messages: list = state.get("messages", [])
        if not messages:
            return END
        last = messages[-1]
        if hasattr(last, "tool_calls") and last.tool_calls:
            return tool_node_name
        return END
    return _condition


# ── Pillar builders ───────────────────────────────────────────────────────────


def build_transaction_graph():
    builder = StateGraph(PillarState)
    builder.add_node("clerk", _clerk_node)
    builder.add_node("transaction_tools", ToolNode(TRANSACTION_TOOLS))
    builder.add_edge(START, "clerk")
    builder.add_conditional_edges("clerk", _make_tools_condition("transaction_tools"))
    builder.add_edge("transaction_tools", "clerk")
    return builder.compile()


def build_analytics_graph():
    builder = StateGraph(PillarState)
    builder.add_node("analyst", _make_pillar_node(ANALYST_PROMPT, "analyst_llm"))
    builder.add_node("analytics_tools", ToolNode(ANALYTICS_TOOLS))
    builder.add_edge(START, "analyst")
    builder.add_conditional_edges("analyst", _make_tools_condition("analytics_tools"))
    builder.add_edge("analytics_tools", "analyst")
    return builder.compile()


def build_ingestion_graph():
    builder = StateGraph(PillarState)
    builder.add_node("engineer", _make_pillar_node(ENGINEER_PROMPT, "engineer_llm"))
    builder.add_node("ingestion_tools", ToolNode(INGESTION_TOOLS))
    builder.add_edge(START, "engineer")
    builder.add_conditional_edges("engineer", _make_tools_condition("ingestion_tools"))
    builder.add_edge("ingestion_tools", "engineer")
    return builder.compile()


def build_chitchat_graph():
    builder = StateGraph(PillarState)
    builder.add_node("qa", _make_pillar_node(QA_PROMPT, "qa_llm"))
    builder.add_edge(START, "qa")
    builder.add_edge("qa", END)
    return builder.compile()
