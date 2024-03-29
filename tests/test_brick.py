"""
As a minimum we want to test:
- output extraction works (tests both prepare and build step)
- deploy step works
"""

from collections import defaultdict, namedtuple
from typing import Any, Dict, List, Tuple, Set
import importlib
import logging
import os
import subprocess

from click.testing import CliRunner, Result
from click.core import Context

from brick.logger import logger, handler
from brick import git

CURRENT_FOLDER = os.path.dirname(os.path.abspath(__file__))
EXAMPLES_FOLDER = os.path.abspath(os.path.join(CURRENT_FOLDER, "../examples"))
EXAMPLE_NODE_FOLDER = os.path.abspath(os.path.join(EXAMPLES_FOLDER, "brick_example_node"))
EXAMPLE_PYTHON_FOLDER = os.path.abspath(os.path.join(EXAMPLES_FOLDER, "brick_example_python"))

OUTPUT_FILE_NODE = os.path.abspath(os.path.join(EXAMPLE_NODE_FOLDER, "dist/out.txt"))
OUTPUT_FILE_PYTHON = os.path.abspath(os.path.join(EXAMPLE_PYTHON_FOLDER, "dist/out.txt"))

logger.removeHandler(handler)

DockerImage = namedtuple("DockerImage", ["tag", "created_at"])


def run_shell_command(cmd: str, check=True):
    return subprocess.run(
        cmd, shell=True, check=check, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def get_docker_images() -> Tuple[Set[str], Dict[str, List[DockerImage]]]:
    result = run_shell_command(
        f'docker images --format "{{{{.Repository}}}};{{{{.Tag}}}};{{{{.CreatedAt}}}}" | grep "brick_example"',
    )

    lines = result.stdout.decode("utf-8").split("\n")
    repositories_to_images = defaultdict(list)
    images = set([])
    for line in lines:
        if line == "":
            continue
        (repository, tag, createdAt) = line.split(";")
        repositories_to_images[repository].append(DockerImage(tag, createdAt))
        images.add(f"{repository}:{tag}")

    return images, repositories_to_images


def get_output_file_content(file_path: str):
    try:
        with open(file_path) as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def clean_up_test_images() -> None:
    run_shell_command(
        "docker images -a | grep 'brick_example' | awk '{print $3}' | xargs docker rmi -f",
        check=False,
    )


def clean_up_output_folders() -> None:
    for path in [OUTPUT_FILE_NODE, OUTPUT_FILE_PYTHON]:
        if os.path.isfile(path):
            os.remove(path)


def invoke_brick_command(monkeypatch, command: str, folder: str, recursive=False) -> Result:
    # pylint: disable import-outside-toplevel

    if recursive:
        assert folder == EXAMPLES_FOLDER

    monkeypatch.chdir(folder)

    # The funky import order and module reloading is due to the monkey patching
    # and the nature of the code running when the modules are imported.

    from brick import lib

    importlib.reload(lib)

    from brick import __main__

    importlib.reload(__main__)

    commands = {
        "build": __main__.build,
        "deploy": __main__.deploy,
        "prepare": __main__.prepare,
        "test": __main__.test,
    }
    command_fn = commands[command]

    parent = Context(command=command_fn)

    if recursive:
        parent.params = {"recursive": True}

    runner = CliRunner()
    result = runner.invoke(command_fn, args=None, parent=parent)

    if result.exception:
        raise result.exception

    assert result.exit_code == 0

    return result


def get_log_messages(caplog: Any, log_level: int) -> List[str]:
    return [r.message for r in caplog.get_records("call") if r.levelno >= log_level]


def get_docker_images_built_from_debug_logs(debug_logs: List[str]) -> Set[str]:
    return {l.split(" ")[-1] for l in debug_logs if "Tagging" in l}


def test_examples_node_build_1_on_master(monkeypatch, caplog) -> None:
    clean_up_test_images()
    clean_up_output_folders()

    monkeypatch.setattr(git, "GIT_BRANCH", "master")

    invoke_brick_command(monkeypatch, command="build", folder=EXAMPLE_NODE_FOLDER)

    debug_logs = get_log_messages(caplog, logging.DEBUG)

    expected_docker_images_built = {
        "brick_example_node_prepare:latest",
        "brick_example_node_prepare:master",
        "brick_example_node_build:latest",
        "brick_example_node_build:master",
        "brick_example_node_prod:1.0",
    }

    assert get_docker_images()[0] == expected_docker_images_built

    assert (
        "Skipping docker build as images are up to date with input dependencies" not in debug_logs
    )

    assert get_docker_images_built_from_debug_logs(debug_logs) == expected_docker_images_built

    assert get_output_file_content(OUTPUT_FILE_NODE) == "hello from node.js"


def test_examples_node_build_2_on_master(caplog, monkeypatch) -> None:
    # NOTE: test depends on test_examples_node_build_1_on_master
    monkeypatch.setattr(git, "GIT_BRANCH", "master")

    invoke_brick_command(monkeypatch, command="build", folder=EXAMPLE_NODE_FOLDER)

    debug_logs = get_log_messages(caplog, logging.DEBUG)

    assert get_docker_images()[0] == {
        "brick_example_node_build:latest",
        "brick_example_node_build:master",
        "brick_example_node_prepare:latest",
        "brick_example_node_prepare:master",
        "brick_example_node_prod:1.0",
    }

    assert "Skipping docker build as images are up to date with input dependencies" in debug_logs

    assert get_docker_images_built_from_debug_logs(debug_logs) == set([])  # nothing was built


def test_examples_node_build_3_on_feature_branch(caplog, monkeypatch) -> None:
    # NOTE: test depends on test_examples_node_build_1_on_master
    clean_up_output_folders()

    monkeypatch.setattr(git, "GIT_BRANCH", "some_branch")

    invoke_brick_command(monkeypatch, command="build", folder=EXAMPLE_NODE_FOLDER)

    debug_logs = get_log_messages(caplog, logging.DEBUG)

    expected_docker_images_built = {
        "brick_example_node_prepare:latest",
        "brick_example_node_prepare:some_branch",
        "brick_example_node_prod:1.0",
        "brick_example_node_build:latest",
        "brick_example_node_build:some_branch",
    }

    docker_images_built_in_previous_tests = {
        "brick_example_node_build:master",
        "brick_example_node_prepare:master",
    }

    assert get_docker_images()[0] == expected_docker_images_built.union(
        docker_images_built_in_previous_tests
    )

    assert (
        not "Skipping docker build as images are up to date with input dependencies" in debug_logs
    )

    promote_logs_without_tag = [
        l.split(":")[0] for l in debug_logs if l.startswith("Promoting image")
    ]

    assert "Promoting image brick_example_node_prepare" in promote_logs_without_tag
    assert (
        "Promoting image brick_example_node_build" in promote_logs_without_tag
        or "Promoting image brick_example_node_prod" in promote_logs_without_tag
    )  # Depending on which image it picks

    assert get_docker_images_built_from_debug_logs(debug_logs) == expected_docker_images_built

    assert get_output_file_content(OUTPUT_FILE_NODE) == "hello from node.js"


def test_workspace_build(monkeypatch, caplog) -> None:
    clean_up_test_images()
    clean_up_output_folders()

    assert get_output_file_content(OUTPUT_FILE_PYTHON) is None

    monkeypatch.setattr(git, "GIT_BRANCH", "master")

    invoke_brick_command(monkeypatch, command="build", folder=EXAMPLES_FOLDER, recursive=True)

    debug_logs = get_log_messages(caplog, logging.DEBUG)

    expected_docker_images_built = {
        "brick_example_node_prepare:latest",
        "brick_example_node_prepare:master",
        "brick_example_node_prod:1.0",
        "brick_example_node_build:latest",
        "brick_example_node_build:master",
        "brick_example_python_prepare:latest",
        "brick_example_python_prepare:master",
        "brick_example_python:latest",
        "brick_example_python:master",
        "brick_example_python_build:latest",
        "brick_example_python_build:master",
    }

    assert get_docker_images()[0] == expected_docker_images_built

    assert get_docker_images_built_from_debug_logs(debug_logs) == expected_docker_images_built

    assert get_output_file_content(OUTPUT_FILE_PYTHON) == "hello from node.js and Python"


def test_workspace_test(monkeypatch, caplog) -> None:
    monkeypatch.setattr(git, "GIT_BRANCH", "master")

    invoke_brick_command(monkeypatch, command="test", folder=EXAMPLES_FOLDER, recursive=True)

    debug_logs = get_log_messages(caplog, logging.DEBUG)

    assert get_docker_images_built_from_debug_logs(debug_logs) == {
        "brick_example_python_test:master",
        "brick_example_python_test:latest",
    }
