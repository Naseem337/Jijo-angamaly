# Rules by sector

One folder per business sector. A new client in the same sector starts from that sector's rules and changes only
what differs (resort names, rooms, prices, phone numbers and policies live in the dashboard / client config, not here).

| Folder | Sector | Main file |
|---|---|---|
| `common/` | General rules for every sector: payment checks, polite and short talking, language, memory | [RULES.md](common/RULES.md) |
| `resort_booking/` | Resorts, homestays, stays (WhatsApp booking bot) | [RULES.md](resort_booking/RULES.md) |

The car-dealer rules are still in [docs/VOICE_AND_REPLY_RULES.md](../docs/VOICE_AND_REPLY_RULES.md).

## Adding a new resort client
1. Read `common/RULES.md` (always) and `resort_booking/RULES.md`: it is the behaviour every resort gets.
2. Put the client's own facts in its dashboard / config (resorts, room types, rates, staff numbers, policies).
3. Anything the client wants different goes in `resort_booking/clients/<client>.md` (create it), never by editing RULES.md.
4. Keep the rules and the code in step: change both, then run `.venv\Scripts\python.exe -m pytest -q tests/resort`.
