"""One guest turn: context, LLM <-> tools (max 6 rounds), then the guard and the outbox.

Empty on purpose in Step AI-1 (docs/AI_AGENT_DESIGN.md section 8); the loop is built in Step AI-3.
"""

from __future__ import annotations
