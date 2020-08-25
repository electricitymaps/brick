export PYTHONPATH=./brick
VENV = .venv
PY_FILES = $(shell find *.py brick -type f -name '*.py')
BLACK_OPTIONS = --line-length=100 brick setup.py
all: $(VENV)


clean:
	find brick -name "*.pyc" -delete
	-rm -rf .*.made build dist *.egg-info


lint: $(VENV) .lint.made

.lint.made: $(PY_FILES) pylintrc
	$(VENV)/bin/pylint brick
	touch $@

pylint: lint



format: $(VENV) .format.made

.format.made: $(PY_FILES)
	$(VENV)/bin/black $(BLACK_OPTIONS)
	touch $@

format-check:
	$(VENV)/bin/black $(BLACK_OPTIONS) --check


typecheck:
	$(VENV)/bin/mypy brick



verify: format lint typecheck
verify-ci: format-check lint typecheck



$(VENV): $(VENV)/.made

$(VENV)/.made: setup.py
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -e .[dev]
	touch $@
