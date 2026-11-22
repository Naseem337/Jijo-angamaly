# Resort booking rules

- [RULES.md](RULES.md): the rules in force (the owner's current rules). Older, replaced rules are deleted; git history keeps them.
- [RULES.pdf](RULES.pdf): a printable copy of RULES.md (regenerate it after big changes).
- Where they live in code: `app/resort/chat.py` (prompt and checks), `flow.py` (the conversation steps),
  `i18n.py` (fixed messages in English, Manglish, Tanglish, Hinglish).
- `clients/` (add when a second resort client needs different rules).
