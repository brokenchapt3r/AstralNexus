# Makefile for AstralNexus

# Variables
PYTHON := python3
PIP := pip
TEST_DIR := tests
SRC_DIR := AstralNexus
VENV := venv

# Default target
.PHONY: help
help:
	@echo "AstralNexus Makefile"
	@echo ""
	@echo "Usage:"
	@echo "  make install     Install dependencies"
	@echo "  make test        Run tests"
	@echo "  make clean       Clean up generated files"
	@echo "  make docs        Generate documentation"
	@echo "  make run         Run the system"
	@echo "  make lint        Run code linting"
	@echo "  make format      Format code with black"

# Install dependencies
.PHONY: install
install:
	$(PIP) install -r $(SRC_DIR)/requirements.txt

# Run tests
.PHONY: test
test:
	$(PYTHON) -m pytest $(TEST_DIR) -v

# Clean up generated files
.PHONY: clean
clean:
	find . -type f -name "*.pyc" -delete
	find . -type d -name "__pycache__" -delete
	rm -rf *.egg-info
	rm -rf .pytest_cache

# Generate documentation
.PHONY: docs
docs:
	@echo "Generating documentation..."
	# Add documentation generation commands here

# Run the system
.PHONY: run
run:
	$(PYTHON) -m AstralNexus

# Run code linting
.PHONY: lint
lint:
	flake8 $(SRC_DIR)

# Format code with black
.PHONY: format
format:
	black $(SRC_DIR)