"""Voice read-back confirmation.

Speech-to-text can mishear "8 lakh" as "80 lakh", so details captured from a voice note
are read back before saving (tools.py stores them as a PendingAction). The customer's
next message decides: a plain yes saves them, anything else discards them and the model
carries on (it can call the tool again with corrected details).
"""

from __future__ import annotations

import json

from app.agent.tools import ToolContext, commit_pending
from app.core.language import classify_confirmation


def resolve_pending(ctx: ToolContext, text: str) -> str | None:
    """Handle a waiting read-back. Returns a note for the model's turn context, or None."""
    action = ctx.repo.get_pending_action(ctx.config.client_id, ctx.phone)
    if action is None:
        return None
    ctx.repo.clear_pending_action(ctx.config.client_id, ctx.phone)
    details = json.dumps(action.args, ensure_ascii=False)
    answer = classify_confirmation(text)
    if answer == "yes":
        result = commit_pending(ctx, action)
        if result.is_error:
            return f"The customer confirmed {action.tool} {details}, but saving failed: {result.content}"
        return (f"The customer confirmed the read-back and {action.tool} was saved: {details}. "
                f"Result: {result.content}. Thank them briefly.")
    if answer == "no":
        return (f"The customer said the read-back of {action.tool} {details} was wrong. "
                "Nothing was saved. Ask what to correct.")
    return (f"A read-back of {action.tool} {details} was waiting for yes/no, but the customer "
            "replied with something else, so it was discarded. If they corrected the details, "
            "call the tool again with the corrected values.")
