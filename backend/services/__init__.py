"""Domain services — pure logic shared by HTTP API and agent tools.

Services consume providers (`providers/`) and return either structured
dataclasses (for the HTTP layer) or pre-rendered strings, via `tools/`'s
LLM-facing facade (for the agent layer). They own classification / decision
rules so the same source of truth feeds both surfaces.
"""
