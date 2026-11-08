# Everything here runs offline and costs nothing, except `record-live`, which calls a live model
# on purpose. `make check` is exactly what CI runs. Without SPEND_GUARD_DATABASE_URL /
# SPEND_GUARD_TEST_DATABASE_URL, a throwaway local PostgreSQL (pgserver) is started.
UV  ?= uv
RUN := $(UV) run --all-extras --all-groups
CLI := $(RUN) llm-spend-guard
CASSETTE := evals/fixtures/live-probe.jsonl
LIVE_CALLS ?= 10
LIVE ?= SPEND_GUARD_PROVIDER=gemini SPEND_GUARD_GEMINI_VERTEX=true SPEND_GUARD_GEMINI_LOCATION=eu \
        SPEND_GUARD_GEMINI_THINKING_LEVEL=low SPEND_GUARD_MAX_LIVE_CALLS=$(LIVE_CALLS)

.PHONY: help install lint fmt typecheck test demo probe-replay record-live serve check

help:            ## list targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/'

install:         ## .venv with all extras, dev tools and the local PostgreSQL (locked)
	$(UV) sync --all-extras --all-groups

lint:            ## ruff lint + format check
	$(RUN) ruff check src tests
	$(RUN) ruff format --check src tests

fmt:             ## apply ruff formatting and safe fixes
	$(RUN) ruff format src tests
	$(RUN) ruff check --fix src tests

typecheck:       ## mypy (strict)
	$(RUN) mypy

test:            ## pytest against a real PostgreSQL
	$(RUN) pytest

demo:            ## offline demo: budget warning, pause, missing price, spans, reconcile
	SPEND_GUARD_PROVIDER=mock $(CLI) demo

probe-replay:    ## replay the recorded live probe through a real ledger; fails on any overrun
	SPEND_GUARD_PROVIDER=replay SPEND_GUARD_CASSETTE=$(CASSETTE) $(CLI) probe

record-live:     ## re-record the probe with the live model (costs money, needs credentials)
	$(LIVE) SPEND_GUARD_RECORD_CASSETTE=$(CASSETTE) $(CLI) probe

serve:           ## HTTP API on http://127.0.0.1:8000 (set SPEND_GUARD_API_TOKEN)
	$(CLI) serve

check: lint typecheck test probe-replay demo  ## everything CI runs
