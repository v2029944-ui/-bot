---
name: python-bug-hunter
description: Perform focused Python bug investigations and risk audits for the MAX Telegram publishing bot. Use for queue races, multi-space authorization, lost drafts, stale callbacks, media issues, LLM costs and duplicate/partial publication.
---

# Python Bug Hunter — publishing bot

## Investigation
Read `AGENTS.md`, `ARCHITECTURE.md`, `SPACES.md`, relevant source and tests. For MAX-specific behavior use the local official `max-messenger` skill and verify time-sensitive API information.

1. Capture the user/space, message ID, draft ID and version, expected versus actual result, and known reproduction steps.
2. Follow the end-to-end path: MAX adapter → durable queue → space context → media/LLM generation → draft and confirmation → MAX/Telegram send → cleanup.
3. Examine SQLite transaction points, retry policies, concurrent workers, signal shutdown, permissions checks, stale UI actions, and ambiguous network outcomes.
4. Build a falsifiable reproduction with temporary SQLite and fake MAX, Telegram and LLM endpoints when feasible.
5. Trace whether another workspace, user role or publication channel is inadvertently affected. Avoid stylistic findings and hypothetical claims presented as fact.
6. Propose minimal fixes, evidence-focused tests and any safe rollback requirements; implement only if requested.

## Critical cases
- Cross-space data disclosure, role escalation, inconsistent admin checks or MAX/Telegram ID mix-ups.
- Double publication after replayed messages, callbacks or unknown network responses.
- Mutations to drafts after partial publishing, or stale edits overwriting newer versions.
- Media lost/reordered or unsafe file/url processing; FFmpeg memory/time limit regressions.
- LLM prompt injection leading to bypass of authorization/publication approval, leaked secrets or unbounded API spending.
- Queue entries stranded or duplicated across restart; data removal before confirmed success.
- Invalid TLS, secret leakage, dangerous deployment or database migration.

## Findings format
Provide exact file/line references, triggering conditions, observed evidence, impact, severity (P0–P3), proposed minimal fix, and an automated test. Separate verified bugs, unconfirmed risks, and untested paths. Do not run real publishing, production Docker commands, paid LLM calls or production DB operations without permission.
