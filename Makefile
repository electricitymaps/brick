export PYTHONPATH=./brick
VENV = .venv
PY_FILES = $(shell find brick tests -type f -name '*.py')
# NOTE: we cannot currently use the pyproject.toml option as installation fails
BLACK_OPTIONS = --target-version=py36 --line-length=100 brick setup.py
all: $(VENV)


clean:
	find brick -name "*.pyc" -delete
	-rm -rf .*.made build dist *.egg-info coverage .pytest_cache $(VENV)


lint: $(VENV) .lint.made

.lint.made: $(PY_FILES) pylintrc
	$(VENV)/bin/pylint brick
	touch $@

pylint: lint



format: $(VENV) .format.made

.format.made: $(PY_FILES) Makefile
	$(VENV)/bin/black $(BLACK_OPTIONS)
	touch $@


format-check:
	$(VENV)/bin/black $(BLACK_OPTIONS) --check


test: $(VENV)
	$(VENV)/bin/py.test -lsvv --cov-report html:coverage --cov=brick tests


verify: format lint test
verify-ci: format-check lint test


$(VENV): $(VENV)/.made

$(VENV)/.made: setup.py
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -e .[dev]
	touch $@
