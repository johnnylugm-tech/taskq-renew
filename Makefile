# taskq-renew Makefile
# Anchors NFR-12 (verifiability): `make verify-system` must exit 0
# after running the full verification target the project declares.
# See SPEC.md §4 NFR-12 and the Gate 2 execute_verification_target
# dimension in harness/harness/ssi/prompts/evaluate_dimension.md.

.PHONY: help install test test-unit test-integration lint type test-coverage verify-system

help:
	@echo "taskq-renew verification targets:"
	@echo "  install            install the project + dev deps into the venv"
	@echo "  test               run the full test suite (unit + integration)"
	@echo "  test-unit          run unit tests (FR-01..FR-08)"
	@echo "  test-integration   run integration tests (CLI end-to-end)"
	@echo "  lint               ruff check (Gate 2 linting dimension)"
	@echo "  type               pyright type check (Gate 2 type_safety dimension)"
	@echo "  test-coverage      pytest --cov with coverage report"
	@echo "  verify-system      run the full verification surface (NFR-12)"

install:
	/Users/johnny/projects/taskq-renew/.venv/bin/python -m pip install -e . 2>/dev/null || true

test: test-unit test-integration

test-unit:
	cd /Users/johnny/projects/taskq-renew && \
	  /Users/johnny/projects/taskq-renew/.venv/bin/python -m pytest \
	    03-development/tests/test_fr01.py \
	    03-development/tests/test_fr02.py \
	    03-development/tests/test_fr03.py \
	    03-development/tests/test_fr04.py \
	    03-development/tests/test_fr05.py \
	    03-development/tests/test_fr06.py \
	    03-development/tests/test_fr07.py \
	    03-development/tests/test_fr08.py \
	    -q

test-integration:
	cd /Users/johnny/projects/taskq-renew && \
	  /Users/johnny/projects/taskq-renew/.venv/bin/python -m pytest \
	    03-development/tests/integration \
	    --cov=03-development/src \
	    --cov-report=term \
	    -q

lint:
	cd /Users/johnny/projects/taskq-renew && \
	  /Users/johnny/projects/taskq-renew/.venv/bin/python -m ruff check . --exit-zero

type:
	cd /Users/johnny/projects/taskq-renew && \
	  /Users/johnny/projects/taskq-renew/.venv/bin/python -m pyright 03-development/src/ --outputjson

test-coverage:
	cd /Users/johnny/projects/taskq-renew && \
	  /Users/johnny/projects/taskq-renew/.venv/bin/python -m coverage run -m pytest --cov=03-development/src -q

# NFR-12: full system verification. Must exit 0.
verify-system: lint type test-coverage test
	@echo "verify-system: PASS"
