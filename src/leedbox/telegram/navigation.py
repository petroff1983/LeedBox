from typing import Any

from aiogram.fsm.context import FSMContext

AGENT_CALLBACK_PREFIXES = ("az:", "ip:", "wp:", "ait:", "ca:", "lm:", "yt:")
AGENT_ACTIVE_TOOLS = frozenset(
    {"agents", "content_agent", "lead_magnet_agent", "writing_profile", "youtube"}
)


async def pause_input(state: FSMContext) -> None:
    """Pause a wizard without deleting answers or cancelling an in-flight operation."""
    if await state.get_state() == "wallet_amount":
        await state.update_data(wallet_offer=None, wallet_nonce=None)
    # Answers live in draft data/ContentTask; a stale input StateFilter must not
    # swallow the next message on the menu. Explicit resume restores the state.
    await state.set_state(None)
    await state.update_data(paused=True)


def can_return_to_expert(data: dict[str, Any]) -> bool:
    """Only a previous server-issued expert session can return from gift preview."""
    return (
        data.get("mode") == "expert"
        or data.get("expert_return_allowed") is True
        # Compatibility for expert screens issued before the return marker existed.
        or bool(data.get("library_item_messages"))
    )
