export PYTHONPATH=./brick
VENV = .venv
PY_FILES = $(shell find brick -type f -name '*.py')


all: $(VENV)


clean:
	find brick -name "*.pyc" -delete
	-rm -rf .*.made build dist *.egg-info


lint: $(VENV) .lint.made

.lint.made: $(PY_FILES) pylintrc
	$(VENV)/bin/pylint brick
	touch $@

pylint: lint



$(VENV): $(VENV)/.made

$(VENV)/.made: setup.py
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -e .[dev]
	touch $@
