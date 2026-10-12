PYTHON ?= python

.PHONY: install test check tui clean
install:
	$(PYTHON) -m pip install -e ".[dev]"
test:
	$(PYTHON) -m pytest tests -m "not live_llm"
check:
	$(PYTHON) -m compileall -q src/AstralNexus
	$(PYTHON) -m pytest tests -m "not live_llm"
tui:
	cd tui && npm ci && npm run build:ink && npm run check && npm run build
clean:
	$(PYTHON) scripts/clean.py
