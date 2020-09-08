export PYTHONPATH=./brick
VENV = .venv
PY_FILES = $(shell find *.py brick -type f -name '*.py')
BLACK_OPTIONS = brick setup.py
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

.format.made: $(PY_FILES) pyproject.toml
	$(VENV)/bin/black $(BLACK_OPTIONS)
	touch $@


format-check:
	$(VENV)/bin/black $(BLACK_OPTIONS) --check


verify: format lint
verify-ci: format-check lint


$(VENV): $(VENV)/.made

$(VENV)/.made: setup.py
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -e .[dev]
	touch $@
