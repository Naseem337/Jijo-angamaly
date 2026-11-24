# Agent progress (update at every commit)

Spec: docs/AI_AGENT_DESIGN.md. Commit format: `Step AI-n: ...`.

## Done
- AI-0 rules + design doc: b7befa4 (price guard follow-up 2ee3542)
- AI-1 skeleton, Enquiry state, dry-run outbox: 0598406
- AI-2a update_enquiry + get_price: 05623a3
- AI-2b photo flow + send_photos: 820c015

## Next
- AI-2c (in progress, uncommitted): remaining tools; tests in tests/resort/test_agent_tools_2c.py
- Then AI-3 prompt + loop, AI-4 gate/guard/outbox, AI-5 evals, AI-6 shadow, AI-7 switch

## Open issues
- AI-2c work not yet committed or fully verified; scope of "remaining tools" to confirm against design doc section 4
