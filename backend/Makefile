.PHONY: run sync-all alembic-rev alembic-up alembic-down pre-commit-install lint ruff mypy clean-cache

# =====================================================
# App
# =====================================================
run:
	uv run uvicorn src.app:app --host 127.0.0.1 --port 8000 --reload

# =====================================================
# Dependencies
# =====================================================
sync-all:
	uv sync --all-groups

# =====================================================
# Database / Alembic
# =====================================================
alembic-rev:
	./alembic_rev.sh "$(m)"

alembic-up:
	uv run alembic upgrade head

alembic-down:
	uv run alembic downgrade -1

# =====================================================
# Code Quality
# =====================================================
pre-commit-install:
	uv run pre-commit install --install-hooks

lint:
	uv run pre-commit run --all-files

ruff:
	uv run ruff check --fix && uv run ruff format .

mypy:
	uv run mypy src --cache-dir .mypy_cache

# =====================================================
# Maintenance
# =====================================================
clean-cache:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type d -name .mypy_cache -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +
	find . -type d -name .ruff_cache -exec rm -rf {} +
