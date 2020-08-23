import os
import tempfile
import subprocess
import sys

from typing import List
import docker
import arrow

from .lib import ROOT_PATH, compute_hash_from_paths
from .logger import logger

docker_client = docker.from_env()


def docker_run(tag, command, volumes=None, ports=None, environment=None):
    cmd = "docker run --rm -ti"
    if volumes:
        cmd += f' {" ".join([f"-v {os.path.abspath(v)}:/home/{os.path.relpath(v, ROOT_PATH)}" for v in volumes])}'
    if ports:
        cmd += f' {" ".join([f"-p {p}:{p}" for p in ports])}'
    if environment:
        cmd += f' {" ".join([f"-e {k}={v}" for k, v in environment.items()])}'

    cmd += f" {tag} {command}"
    sys.exit(subprocess.run(cmd, shell=True, check=False).returncode)


def docker_build(
    tags: List[str],
    dockerfile_contents: str,
    pass_ssh=False,
    no_cache=False,
    secrets=None,
    dependency_paths=None,
) -> str:
    # pylint: disable=too-many-branches
    tag_to_return = tags[-1]  # Not sure why we return an argument the caller provided

    # Optimization: Skip builds if the hash of dependencies did not change since the last build.
    # Even though Buildkit is fairly fast at verifying that nothing changed, there is still a 1
    # second overhead for each image (steps: "resolve image config for" + "load metadata for").
    # Performance example: When everything is cached this gives a 3.5X speedup locally for 38 targets. (180 to 52 seconds)
    dependency_hash = compute_hash_from_paths(dependency_paths) if dependency_paths else None
    if dependency_hash:
        dockerfile_contents += f'\nLABEL brick.dependency_hash="{dependency_hash}"'
        images = get_image_names_with_dependency_hash(dependency_hash)
        images_are_up_to_date = set(tags).issubset(set(images))
        if images_are_up_to_date:
            logger.debug(f"Skipping docker build as images are up to date with input dependencies")
            return tag_to_return

    dockerfile_path = os.path.join(ROOT_PATH, ".brickdockerfile")
    if os.path.exists(dockerfile_path):
        logger.warning(f"{dockerfile_path} already exists at root of workspace")
        os.remove(dockerfile_path)
    with open(dockerfile_path, "w+") as dockerfile:
        dockerfile.write(dockerfile_contents)
    try:
        iidfile = tempfile.mktemp()
        cmd = f"docker build . --iidfile {iidfile} -f {dockerfile_path}"
        env = {"DOCKER_BUILDKIT": "1"}
        if pass_ssh:
            cmd += " --ssh default"
            env["SSH_AUTH_SOCK"] = os.environ["SSH_AUTH_SOCK"]
        if no_cache:
            cmd += " --no-cache"
        for k, v in (secrets or {}).items():
            src = os.path.expanduser(v["src"])
            # cmd += f' --secret id={k},src={src}'
            # For now we tar the whole secrets directory
            # as buildkit doesn't support mounting directories
            # See https://github.com/moby/buildkit/issues/970
            basename = os.path.basename(src)
            tarfile = os.path.join(ROOT_PATH, f"{basename}.tar.gz")
            subprocess.run(
                f"tar zc -C {src} --exclude='logs' . > {tarfile}", shell=True, check=True,
            )
            cmd += f" --secret id={k},src={tarfile}"
        with subprocess.Popen(
            args=cmd,
            encoding="utf8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=True,
            env=env,
            universal_newlines=True,
            cwd=ROOT_PATH,
        ) as p:
            logs = [cmd]
            logger.debug(cmd)
            while True:
                line = p.stderr.readline()  # type: ignore
                if line == "":
                    break
                log = line.rstrip("\n")
                logs += [log]
                logger.debug(log)
            returncode = p.wait()
            if returncode:
                _out, err = p.communicate()
                logger.error("\n".join(logs))
                logger.error(err)
                sys.exit(returncode)
            os.remove(dockerfile_path)
    except (KeyboardInterrupt, SystemExit):
        os.remove(dockerfile_path)
        raise
    finally:
        # Cleanup tar files
        for k, v in (secrets or {}).items():
            src = os.path.expanduser(v["src"])
            tarfile = os.path.join(ROOT_PATH, f"{basename}.tar.gz")
            os.remove(tarfile)

    with open(iidfile) as f:
        digest = f.readline().split(":")[1].strip()
    os.remove(iidfile)

    image = docker_client.images.get(digest)
    for tag in tags:
        logger.debug(f"Tagging {tag}..")
        repository, version = tag.split(":")
        image.tag(repository=repository, tag=version)

    return tag_to_return


def docker_images_list(name, last_tagged_before=None):
    return [
        {
            "id": x.attrs["Id"],
            "tags": x.tags,
            "size": x.attrs["Size"],
            "lastTagTime": x.attrs["Metadata"]["LastTagTime"],
        }
        for x in docker_client.images.list(f"{name}_*")
        if not last_tagged_before
        or arrow.get(x.attrs["Metadata"]["LastTagTime"]) < arrow.get(last_tagged_before)
    ]


def get_image_names_with_dependency_hash(dependency_hash) -> List[str]:
    images = (
        subprocess.run(
            f"docker images --filter \"label=brick.dependency_hash={dependency_hash}\" --format '{{{{.Repository}}}}:{{{{.Tag}}}}'",
            shell=True,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        .stdout.decode("utf-8")
        .strip()
    )

    return images.split("\n")


def docker_image_delete(image_id, force=False):
    docker_client.images.remove(image=image_id, noprune=False, force=force)
