"""Retrieval over runbooks, postmortems and architecture docs.

Only one of Faultline's four knowledge sources is classic document RAG, and this
is it. Change events are a SQL query over a time window, live telemetry is a tool
call, and similar incidents match on a structured signature. Embedding every log
line would be expensive and semantically weak; embedding a runbook is exactly
right.
"""
