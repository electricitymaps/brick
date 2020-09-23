import pytest

from brick.lib import expand_brick_environment_variables


def test_expand_brick_environment_variables(monkeypatch):
    assert expand_brick_environment_variables("") == ""

    # Supports default if BRICK_ variable is not found
    assert (
        expand_brick_environment_variables("tag: server:${BRICK_COMMIT_SHA:-latest}")
        == "tag: server:latest"
    )

    # Should expand BRICK_ variables if found
    monkeypatch.setenv("BRICK_COMMIT_SHA", "1234")
    assert (
        expand_brick_environment_variables("tag: server:${BRICK_COMMIT_SHA:-latest}")
        == "tag: server:1234"
    )

    assert (
        expand_brick_environment_variables("tag: server:${BRICK_COMMIT_SHA}") == "tag: server:1234"
    )

    # Should not expand non BRICK_ prefixed variables
    assert (
        expand_brick_environment_variables("tag: server:${COMMIT_SHA:-latest}")
        == "tag: server:${COMMIT_SHA:-latest}"
    )

    # Raises exception if unsupported format is used
    with pytest.raises(AssertionError) as excinfo:
        expand_brick_environment_variables("tag: server:$BRICK_FOO")
    assert "faulty BRICK_ environment" in str(excinfo.value)

    # Raises exception if no default value is provided
    with pytest.raises(AssertionError) as excinfo:
        expand_brick_environment_variables("tag: server:${BRICK_FOO}")
    assert "not find environment variable BRICK_FOO or default value" in str(excinfo.value)
