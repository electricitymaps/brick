import os

import pytest

CURRENT_FOLDER = os.path.dirname(os.path.abspath(__file__))


def pytest_sessionstart():
    # To avoid recurssion issue in lib.py (side-effects!)
    os.chdir(CURRENT_FOLDER)

    from brick.logger import logger, handler

    logger.removeHandler(handler)

