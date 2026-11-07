---
name: regression-testing
description: Write and run regression tests for the MAX-to-MAX/Telegram publishing bot. Use for material ingestion, multi-space permissions, stale callbacks, SQLite queues, LLM drafts, media processing, partial publication or restart bugs.
---

# Regression testing — publishing bot

## Context
Read `AGENTS.md`, `ARCHITECTURE.md`, `SPACES.md`, relevant `services/` modules and existing `tests/`. Runtime uses Python 3.12, pytest, SQLite, a custom MAX HTTP adapter, aiogram, media tools and external LLM APIs. Use `.agents/skills/max-messenger/SKILL.md` when checking MAX API details.

## Red–green workflow
1. Capture the initial material, space, roles, draft/version, publication states and expected behavior.
2. Trace reception via `max_queue.py` / `max_actions.py`, storage and `services/`, with `spaces.py` context.
3. Write a focused test that reproduces the defect using temporary databases, fake adapters and controlled async scheduling. Demonstrate failure before changing code whenever possible.
4. Make only the approved code change. Keep existing role and publication semantics.
5. Run the focused test, related tests and full automated checks. Verify the test catches the underlying bug.
6. Report exact commands and actual results; identify remaining real-channel and media integration checks.

## Regression matrix
- Same incoming MAX message or repeated button leads to only one actionable draft/publication.
- Stale draft versions and previous confirmation buttons cannot overwrite current work.
- Users/admins cannot access another workspace's drafts, settings, members or channels; distinguish MAX from Telegram IDs.
- SQLite or LLM failures do not silently discard materials or generate duplicate paid requests without notice.
- Simulated timeout after one successful channel prevents blind resend to that channel; unknown outcomes require manual verification.
- Media captions, album order and files survive retry, partial processing and restart.
- Concurrent workers maintain per-space isolation and global resource limits.
- Shutdown and reload preserve persisted in-flight work without unsafe duplicate side effects.

## Available checks
From root in a suitable environment with the project's pinned development dependencies:

```sh
python -m pytest -ra
ruff check .
mypy
```

For integration-specific changes also use repository GitHub Actions checks, including container build and MAX TLS-context validation. Never send to production MAX/Telegram channels, access real data, or make paid LLM calls without authorization. Mocked tests do not prove real delivery.
