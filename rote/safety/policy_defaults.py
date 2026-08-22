from __future__ import annotations

from rote.contracts.common import Currency, ExceptionCategory
from rote.contracts.policy import (
    ExecutionPath,
    MoneyArgument,
    PolicyConfig,
    PolicyRule,
)

READ_TOOLS = frozenset(
    {
        "get_settlement_record",
        "get_bank_line",
        "find_bank_lines_by_amount",
        "list_bank_lines_for_reference",
        "get_fee_schedule",
        "get_fx_rate",
    }
)

WRITE_TOOLS = frozenset({"post_adjustment", "mark_settlement_matched", "void_duplicate_bank_line"})

# read-only, typed, non-sensitive and never needed to resolve a case. Exposed on purpose: if
# the agent could only reach the tools it needs, a recorded tool choice would not be a choice
# at all, and the Risk R2 defence in ARCHITECTURE.md would be switched off by the allowlist.
OBSERVATIONAL_TOOLS = frozenset(
    {"get_merchant_profile", "get_chargeback_history", "recalculate_settlement_batch"}
)

BASELINE_PER_ACTION = {Currency.INR: 50_000, Currency.USD: 1_000}
BASELINE_PER_WINDOW = {Currency.INR: 2_000_000, Currency.USD: 40_000}
WINDOW_SECONDS = 3_600

# categories that lean hardest on merchant free text carry the lowest caps, because a nudged
# label is the injection that actually works: see ARCHITECTURE.md SS F/T2
CATEGORY_PER_ACTION: dict[ExceptionCategory, dict[Currency, int]] = {
    ExceptionCategory.PARTIAL_PAYMENT: {Currency.INR: 20_000, Currency.USD: 400},
    ExceptionCategory.DUPLICATE_ENTRY: {Currency.INR: 20_000, Currency.USD: 400},
    ExceptionCategory.FEE_MISMATCH: {Currency.INR: 40_000, Currency.USD: 800},
    ExceptionCategory.FX_ROUNDING: {Currency.INR: 5_000, Currency.USD: 100},
    ExceptionCategory.TIMING_CUTOFF: {Currency.INR: 5_000, Currency.USD: 100},
    ExceptionCategory.TRANSPOSED_REFERENCE: {Currency.INR: 5_000, Currency.USD: 100},
}

CATEGORY_TOOLS: dict[ExceptionCategory, frozenset[str]] = {
    ExceptionCategory.FEE_MISMATCH: READ_TOOLS | {"post_adjustment", "mark_settlement_matched"},
    ExceptionCategory.FX_ROUNDING: READ_TOOLS | {"post_adjustment", "mark_settlement_matched"},
    ExceptionCategory.PARTIAL_PAYMENT: READ_TOOLS | {"post_adjustment", "mark_settlement_matched"},
    ExceptionCategory.TIMING_CUTOFF: READ_TOOLS | {"mark_settlement_matched"},
    ExceptionCategory.TRANSPOSED_REFERENCE: READ_TOOLS | {"mark_settlement_matched"},
    ExceptionCategory.DUPLICATE_ENTRY: READ_TOOLS
    | {"mark_settlement_matched", "void_duplicate_bank_line"},
}


def default_policy_config() -> PolicyConfig:
    rules: list[PolicyRule] = []
    for path in ExecutionPath:
        # amendment A2: both paths start from the identical fallback, with no implicit privilege
        rules.append(
            PolicyRule(
                path=path,
                category=None,
                allowed_tools=READ_TOOLS | WRITE_TOOLS | OBSERVATIONAL_TOOLS,
                max_per_action=dict(BASELINE_PER_ACTION),
                max_per_window=dict(BASELINE_PER_WINDOW),
                window_seconds=WINDOW_SECONDS,
            )
        )
        for category in ExceptionCategory:
            if category not in CATEGORY_PER_ACTION:
                continue
            rules.append(
                PolicyRule(
                    path=path,
                    category=category,
                    allowed_tools=CATEGORY_TOOLS[category] | OBSERVATIONAL_TOOLS,
                    max_per_action=dict(CATEGORY_PER_ACTION[category]),
                    max_per_window=dict(BASELINE_PER_WINDOW),
                    window_seconds=WINDOW_SECONDS,
                )
            )
    return PolicyConfig(
        rules=tuple(rules),
        money_arguments=(
            MoneyArgument(
                tool="post_adjustment", amount_arg="minor_units", currency_arg="currency"
            ),
        ),
        require_idempotency_for=WRITE_TOOLS,
    )
