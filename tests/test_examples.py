"""
As a minimum we want to test:
- output extraction works (tests both prepare and build step)
- deploy step works
"""

import os
from collections import namedtuple
from multiprocessing import Queue
import logging
import pytest
import subprocess
from collections import defaultdict
from typing import Any, Callable, Dict, Generator, List, Tuple

from brick.logger import logger, handler
from click.testing import CliRunner
from click.core import Context

CURRENT_FOLDER = os.path.dirname(os.path.abspath(__file__))
EXAMPLES_FOLDER = os.path.join(CURRENT_FOLDER, "../examples")
EXAMPLE_NODE_FOLDER = os.path.join(EXAMPLES_FOLDER, "brick-example-node")

logger.removeHandler(handler)

DockerImage = namedtuple("DockerImage", ["tag", "created_at"])


def get_docker_images() -> Dict[str, List[DockerImage]]:
    result = subprocess.run(
        f'docker images --format "{{{{.Repository}}}};{{{{.Tag}}}};{{{{.CreatedAt}}}}" | grep "brick-example"',
        shell=True,
        check=True,
        capture_output=True,
    )

    lines = result.stdout.decode("utf-8").split("\n")
    repositories = defaultdict(list)
    for line in lines:
        if line == "":
            continue
        (repository, tag, createdAt) = line.split(";")
        repositories[repository].append(DockerImage(tag, createdAt))

    return repositories


@pytest.fixture(autouse=True)
def before_each() -> None:
    subprocess.run(
        f"docker rmi -f $(docker images | grep 'brick-example')",
        shell=True,
        check=False,
        capture_output=True,
    )


GetLogMessages = Callable[[], List[str]]


@pytest.fixture
def get_log_messages(caplog: Any) -> Generator[GetLogMessages, None, None]:
    with caplog.at_level(logging.INFO):

        def get() -> List[str]:
            return [r.message for r in caplog.get_records("call")]

        yield get


def test_examples_node_build(get_log_messages: GetLogMessages) -> None:
    os.chdir(EXAMPLE_NODE_FOLDER)
    runner = CliRunner()
    # pylint: disable import-outside-toplevel
    from brick.__main__ import build

    parent = Context(command=build)
    result = runner.invoke(build, args=None, parent=parent)

    if result.exception:
        raise result.exception

    assert result.exit_code == 0
    assert get_log_messages() == [
        "🔨 Preparing brick-example-node..",
        "💯 Preparation phase done!",
        "🔨 Building brick-example-node..",
        "💯 Finished building brick-example-node!",
    ]

    assert sorted(get_docker_images()) == sorted(
        ["brick-example-node", "brick-example-node_build", "brick-example-node_prepare",]
    )
