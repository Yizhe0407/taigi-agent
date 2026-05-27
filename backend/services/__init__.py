"""Domain services — pure logic shared by HTTP API and agent tools.

Services consume providers (`tools/`) and return either structured dataclasses
(for the HTTP layer) or pre-rendered strings (for the LLM agent layer). They
own classification / decision rules so the same source of truth feeds both
surfaces.
"""
