import subprocess

from .logger import logger

_sha1_command = None


def run_shell_command(cmd: str, cwd: str = None) -> str:
    return subprocess.check_output(cmd, shell=True, encoding="utf8", cwd=cwd).rstrip("\n")


def get_sha1_command() -> str:
    """Returns the fastest sha1sum command supported by the system"""
    # pylint: disable=global-statement
    global _sha1_command
    if not _sha1_command:
        try:
            run_shell_command("which sha1sum")
            _sha1_command = "sha1sum"
        except subprocess.CalledProcessError:
            logger.info("sha1sum not found on this system (performance will be slightly degraded)")
            _sha1_command = "shasum"
    return _sha1_command
