from __future__ import annotations

from rote.contracts.canonical import canonical_bytes
from rote.contracts.common import UntrustedText
from rote.contracts.execution import AgentHandoff, Handover

DIVERGING_RESULT_PATH = "$.diverging_tool_result"


# a diverging tool result is exactly the poisoning vector, so it crosses to the live agent as
# untrusted text and never as trusted state
def build_handoff(
    handover: Handover, *, original_untrusted: tuple[UntrustedText, ...]
) -> AgentHandoff:
    blocks = list(original_untrusted)
    if handover.untrusted_result is not None:
        blocks.append(
            UntrustedText.of(
                DIVERGING_RESULT_PATH, canonical_bytes(handover.untrusted_result).decode()
            )
        )
    return AgentHandoff(
        task_input=dict(handover.state.task_input),
        untrusted=tuple(blocks),
        resumed_from_step=handover.step_index,
        reason=handover.reason,
    )
