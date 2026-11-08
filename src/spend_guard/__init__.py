"""A budget guard for LLM calls: reserve the worst-case cost in PostgreSQL before each call, settle
the actual cost after it, and refuse calls without a checked price."""

__version__ = "0.1.0"
