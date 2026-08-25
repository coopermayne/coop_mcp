"""The learning log (spaced repetition), ported from the standalone `teacher` repo.

Same architecture as the rest of this repo: the store schedules and records,
the model composes questions and judges answers at review time. server.py wraps
these functions as the third MCP instance (`teacher_mcp`); the webapp reads
them for the /learn pages. Three seams differ from the original teacher repo:
the DB path is JOURNAL_DB (shared file), day boundaries are Pacific rather than
system-local, and the FTS table is `learn_fts` (a shared DB has no room for a
name as generic as `search_fts`).
"""
