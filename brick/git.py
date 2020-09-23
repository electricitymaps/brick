import subprocess

# Git branch name with some replacement for making it Docker repository friendly
GIT_BRANCH = subprocess.check_output(
    "git rev-parse --abbrev-ref HEAD | " r"sed 's/\//\-/' | sed 's/ *//g'",
    shell=True,
    encoding="utf8",
).rstrip("\n")

MAIN_BRANCH = subprocess.check_output(
    "git symbolic-ref refs/remotes/origin/HEAD | sed 's@^refs/remotes/origin/@@'",
    shell=True,
    encoding="utf8",
).rstrip("\n")
