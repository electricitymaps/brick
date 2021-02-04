#!/usr/bin/env python3

import logging
import os
import shutil
import time
import io
import tarfile

import arrow
import click
import docker
from wcmatch import wcmatch

from .dockerlib import (
    docker_run,
    docker_build,
    docker_images_list,
    docker_image_delete,
)
from .lib import (
    get_config,
    get_relative_config_path,
    expand_inputs,
    ROOT_PATH,
    intersecting_outputs,
    get_build_repository_and_tag,
)
from .git import GIT_BRANCH
from .logger import logger, handler

docker_client = docker.from_env()

# Folder exclude patterns separated by |. (e.g. 'node_modules|dist|whatever')
GLOB_EXCLUDES = "node_modules"

YARN_CACHE_LOCATION = "/usr/local/share/.cache/yarn"
IMAGES_TO_YARN_CACHE_VERSION_DICT = {
    "node:8.11": "v1",
    "node:8.14.0": "v4",
    "node:10.3": "v1",
    "node:10.13": "v2",
    "node:10.13.0": "v2",
    "node:10.15.3": "v4",
    "node:10.19.0-alpine": "v6",
    "node:12.13.1": "v6",
}


def is_yarn_install_command(cmd):
    install_commands = ["yarn", "yarn install"]
    # Strip flags and trim the string
    split = cmd.split("--")
    clean_command = split[0].strip()
    return clean_command in install_commands


timings = []
cyan = "\x1b[36;21m"
green = "\x1b[32;21m"
yellow = "\x1b[33;21m"
red = "\x1b[31;21m"
reset = "\x1b[0m"


def log_exec_time(task, target, start):
    end = time.perf_counter()
    duration = round(end - start, 2)
    duration_color = red if duration > 10 else green
    text = f"  {duration_color}{duration}s{reset} - {cyan}{task}{reset} of {yellow}{target}{reset}"
    timings.append(text)


def compute_tags(name, step_name):
    return add_version_to_tag(f"{name}_{step_name}")


def add_version_to_tag(name):
    assert ":" not in name, f"Did not expect any tags in {name}"
    latest_tag = f"{name}:latest"
    branch_tag = f"{name}:{GIT_BRANCH.replace('#', '').replace('/', '-')}"
    # Last tag should be the most specific
    return [
        latest_tag,
        branch_tag,
    ]


def get_name(target_rel_path):
    return target_rel_path.replace("/", "_")


def generate_dockerfile_contents(
    from_image,
    inputs,
    commands,
    workdir,
    entrypoint=None,
    inputs_from_build=None,
    pass_ssh=False,
    secrets=None,
    external_images=None,
    environment=None,
    chown=None,
):
    dockerfile_contents = "# syntax = docker/dockerfile:experimental\n"
    dockerfile_contents += f"FROM {from_image}\n"

    copy_flag_chown = f"--chown={chown}" if chown else ""

    if inputs_from_build:
        dockerfile_contents += (
            "\n".join(
                [
                    f"COPY {copy_flag_chown} --from={x[0]} /home/{x[1]} /home/{x[1]}"
                    for x in inputs_from_build
                ]
            )
            + "\n"
        )
    dockerfile_contents += (
        "\n".join([f'COPY {copy_flag_chown} ["{x}", "/home/{x}"]' for x in inputs]) + "\n"
    )
    # External images
    # https://docs.docker.com/develop/develop-images/multistage-build/#use-an-external-image-as-a-stage
    for k, v in (external_images or {}).items():
        dockerfile_contents += (
            f'COPY {copy_flag_chown} --from={v["tag"]} {v["src"]} {v["target"]}\n'
        )

    dockerfile_contents += f"WORKDIR /home/{workdir or ''}\n"
    run_flags = []
    if pass_ssh:
        run_flags += ["--mount=type=ssh"]
    for k, v in (secrets or {}).items():
        # run_flags += [f'--mount=type=secret,id={k},target={v["target"]},required']
        # Use the tar file passed instead
        # Note: One could use --mount-type=bind if the secrets
        # were placed in the build context
        run_flags += [f'--mount=type=secret,id={k},target={v["target"]}.tar.gz,required']

    for k, v in (environment or {}).items():
        dockerfile_contents += f"ENV {k}='{v}'\n"

    def generate_run_command(cmd, run_flags):
        cache_version = IMAGES_TO_YARN_CACHE_VERSION_DICT.get(from_image)
        if is_yarn_install_command(cmd) and cache_version:
            location = f"{YARN_CACHE_LOCATION}/{cache_version}"
            logger.debug(f"Using yarn cache located at {location}")
            run_flags += [f"--mount=type=cache,target={location}"]
        if (secrets or {}).items():
            # Wrap the run command with a tar command
            # to untar and cleanup after us
            pre = " && ".join(
                [
                    f'mkdir -p {v["target"]} && tar zxf {v["target"]}.tar.gz -C{v["target"]}'
                    for k, v in (secrets or {}).items()
                ]
            )
            post = " && ".join([f'ls  && rm -rf {v["target"]}' for k, v in (secrets or {}).items()])
            return f"""RUN {' '.join(run_flags)} \
                       {pre} && \
                       {cmd} && \
                       {post}
                    """
        else:
            return f"RUN {' '.join(run_flags + [cmd])}"

    dockerfile_contents += (
        "\n".join([generate_run_command(cmd, run_flags) for cmd in commands]) + "\n"
    )
    # Add entrypoint
    if entrypoint:
        dockerfile_contents += f"CMD {entrypoint}"

    return dockerfile_contents


def check_recursive(ctx, target, fun):
    if ctx.parent.params.get("recursive"):
        start = time.perf_counter()
        targets = [
            os.path.dirname(x)
            for x in sorted(
                wcmatch.WcMatch(
                    f"{target}", "BUILD.yaml", GLOB_EXCLUDES, flags=wcmatch.RECURSIVE
                ).match()
            )
        ]
        logger.info(f"Found {len(targets)} target(s)..")
        for recursive_target in targets:
            # Note: the recursive parameter will not be passed
            # and thus the recursion will end here
            ctx.invoke(
                fun,
                target=recursive_target,
                skip_previous_steps=ctx.parent.params.get("skip_previous_steps"),
            )
        end = time.perf_counter()
        logger.info(f"🌟 All targets finished in {round(end - start, 2)} seconds")
        logger.info("Detailed timing:")
        for timing in timings:
            logger.info(timing)

        return True


def image_exists(tag):
    try:
        docker_client.images.get(tag)
        return True
    except docker.errors.ImageNotFound:
        return False


@click.group()
@click.option("--skip-previous-steps", help="skips previous steps", is_flag=True)
@click.option("--verbose", help="verbose", is_flag=True)
@click.option("-r", "--recursive", help="recursive", is_flag=True)
def cli(verbose, recursive, skip_previous_steps):
    if skip_previous_steps:
        logger.debug(f"Skipping previous steps if possible..")

    if verbose:
        handler.setLevel(logging.DEBUG)
    else:
        handler.setLevel(logging.INFO)


@cli.command("list")  # NOTE: to not redefining built-in 'list'
@click.argument("target", default=".")
@click.pass_context
def list_(ctx, target):
    targets = [
        os.path.dirname(x)
        for x in sorted(
            wcmatch.WcMatch(
                f"{target}", "BUILD.yaml", GLOB_EXCLUDES, flags=wcmatch.RECURSIVE
            ).match()
        )
    ]
    logger.info(f"Found {len(targets)} target(s):")
    for t in targets:
        logger.info(t)


@cli.command()
@click.argument("target", default=".")
@click.argument("skip_previous_steps", default=False)
@click.pass_context
def prepare(ctx, target, skip_previous_steps):
    if check_recursive(ctx, target, prepare):
        return

    start_time = time.perf_counter()
    target_rel_path = os.path.relpath(target, start=ROOT_PATH)
    config = get_config(target)
    steps = config["steps"]
    name = get_name(target_rel_path)

    if "prepare" not in steps:
        logger.info("Nothing to prepare")
        return

    step = steps["prepare"]
    inputs = expand_inputs(target_rel_path, step.get("inputs", []))
    dependency_paths = inputs + [get_relative_config_path(target)]
    dockerfile_contents = generate_dockerfile_contents(
        from_image=step["image"],
        inputs=inputs,
        commands=step.get("commands", []),
        environment=step.get("environment", {}),
        workdir=target_rel_path,
        chown=step.get("chown"),
    )

    # Docker build
    logger.info(f"🔨 Preparing {target_rel_path}..")
    tags = compute_tags(name, "prepare")
    digest, is_cached = docker_build(
        tags=tags, dependency_paths=dependency_paths, dockerfile_contents=dockerfile_contents
    )
    logger.info(f"💯 Preparation phase done{' (cached)' if is_cached else ''}!")
    log_exec_time("prepare", target_rel_path, start_time)
    # TODO: For some reason, buildkit doesn't support FROM with digests
    return digest


@cli.command()
@click.argument("target", default=".")
@click.argument("skip_previous_steps", default=False)
@click.pass_context
def build(ctx, target, skip_previous_steps):
    if check_recursive(ctx, target, build):
        return

    start_time = time.perf_counter()
    target_rel_path = os.path.relpath(target, start=ROOT_PATH)
    config = get_config(target)
    steps = config["steps"]
    name = get_name(target_rel_path)

    # Run the previous step if required
    prepare_tag = compute_tags(name, "prepare")[-1]
    should_run_prepare = not skip_previous_steps or not image_exists(prepare_tag)
    if should_run_prepare:
        prepare_tag = ctx.invoke(prepare, target=target)

    step = steps["build"]
    logger.info(f"🔨 Building {target_rel_path}..")

    # Build dependencies
    dependencies = intersecting_outputs(target_rel_path, step.get("inputs", []))
    if dependencies:
        logger.debug(f"Found dependencies: {dependencies}")
        for dependency in dependencies:
            logger.info(f"➡️  Building dependency {dependency}")
            ctx.invoke(build, target=os.path.join(ROOT_PATH, dependency))

    # Note build dependencies must be done pre-glob
    # as else globs might return nothing (if they have not been built)
    inputs = expand_inputs(target_rel_path, step.get("inputs", []))
    dependency_paths = inputs + [get_relative_config_path(target)]

    # If no digest is given (because there's no build step)
    # use current image instead
    digest = prepare_tag or step["image"]
    dockerfile_contents = generate_dockerfile_contents(
        from_image=digest,
        inputs=inputs,
        commands=step.get("commands", []),
        entrypoint=step.get("entrypoint"),
        environment=step.get("environment", {}),
        external_images=step.get("external_images"),
        workdir=target_rel_path,
        chown=step.get("chown"),
    )

    # Docker build
    build_image_name = step.get("tag", None)

    # tags consists of intermediate Brick steps + either build.tag or the name of the package

    if build_image_name:
        additional_tags = (
            [build_image_name] if ":" in build_image_name else add_version_to_tag(build_image_name)
        )
    else:
        additional_tags = add_version_to_tag(name)

    tags = compute_tags(name, "build") + additional_tags

    digest, is_cached = docker_build(
        tags=tags, dependency_paths=dependency_paths, dockerfile_contents=dockerfile_contents
    )

    # TODO: We could skip gathering the output if build did not run AND output folders are up to date

    # Gather output
    for output in step.get("outputs", []):
        logger.debug(f"Collecting {os.path.join(target_rel_path, output)} from {digest}")
        # Make sure we check that outputs are in this folder,
        # as else the dependency system won't work
        if os.path.abspath(os.path.join(ROOT_PATH, target_rel_path)) not in os.path.abspath(
            os.path.join(ROOT_PATH, target_rel_path, output)
        ):
            raise Exception(f"Output {output} is not in current folder")

        host_path = os.path.join(ROOT_PATH, target_rel_path, output)
        container_path = f"/home/{os.path.join(target_rel_path, output)}"
        if os.path.exists(host_path):
            if os.path.isdir(host_path):
                shutil.rmtree(host_path)
            else:
                os.remove(host_path)

        # Pull out the outputs to the host
        container = docker_client.containers.run(image=digest, remove=True, detach=True)
        try:
            archive_bits, _stats = container.get_archive(container_path)
        except docker.errors.NotFound:
            # We see periodic failures, not sure if we should restart the container?
            logger.info(f"Collecting failed, retrying")
            time.sleep(2)
            archive_bits, _stats = container.get_archive(container_path)

        with io.BytesIO() as archive_tar_content:
            for chunk in archive_bits:
                archive_tar_content.write(chunk)
            archive_tar_content.seek(0)
            with tarfile.open(fileobj=archive_tar_content, mode="r:") as tar:
                host_output_folder = os.path.abspath(os.path.join(host_path, "../"))
                tar.extractall(host_output_folder)

    logger.info(f"💯 Finished building {target_rel_path}{' (cached)' if is_cached else ''}!")
    log_exec_time("build", target_rel_path, start_time)
    return digest


@cli.command()
@click.argument("target", default=".")
@click.argument("skip_previous_steps", default=False)
@click.pass_context
def test(ctx, target, skip_previous_steps):
    if check_recursive(ctx, target, test):
        return

    start_time = time.perf_counter()
    target_rel_path = os.path.relpath(target, start=ROOT_PATH)
    config = get_config(target)
    steps = config["steps"]
    name = get_name(target_rel_path)

    if "test" not in steps:
        logger.info("Nothing to test")
        return

    build_tag = compute_tags(name, "build")[-1]
    should_run_build = not skip_previous_steps or not image_exists(build_tag)
    if should_run_build:
        build_tag = ctx.invoke(build, target=target)

    step = steps["test"]
    inputs = expand_inputs(target_rel_path, step.get("inputs", []))
    dockerfile_contents = generate_dockerfile_contents(
        from_image=build_tag,
        inputs=inputs,
        commands=step.get("commands", []),
        workdir=target_rel_path,
        environment=step.get("environment", {}),
        chown=step.get("chown"),
    )

    # Docker build
    logger.info(f"🔍 Testing {target_rel_path}..")
    digest, is_cached = docker_build(
        tags=compute_tags(name, "test"),
        dependency_paths=None,  # always run tests
        dockerfile_contents=dockerfile_contents,
    )
    logger.info(f"✅ Tests passed{' (cached)' if is_cached else ''}!")
    log_exec_time("test", target_rel_path, start_time)
    return digest


@cli.command()
@click.argument("target", default=".")
@click.argument("skip_previous_steps", default=False)
@click.option("--no-cache", default=False, is_flag=True, help="skip caching deployment")
@click.pass_context
def deploy(ctx, target, skip_previous_steps, no_cache):
    if check_recursive(ctx, target, deploy):
        return

    target_rel_path = os.path.relpath(target, start=ROOT_PATH)
    config = get_config(target)
    steps = config["steps"]
    name = get_name(target_rel_path)

    if "deploy" not in steps:
        logger.info("Nothing to deploy")
        return

    step = steps["deploy"]

    # Check if it should run previous step
    if "test" in steps:
        previous_tag = compute_tags(name, "test")[-1]
        should_run_test = not skip_previous_steps or not image_exists(previous_tag)
        if should_run_test:
            previous_tag = ctx.invoke(test, target=target)
    elif "build" in steps:
        previous_tag = compute_tags(name, "build")[-1]
        should_run_build = not skip_previous_steps or not image_exists(previous_tag)
        if should_run_build:
            previous_tag = ctx.invoke(build, target=target)
    else:
        raise Exception("Could not detect previous step")

    # Push image if needed
    if step.get("push_image") is True:
        repository_and_tag = get_build_repository_and_tag(steps)
        assert repository_and_tag, "Expected build.tag when push_image was used"
        repository, tag = repository_and_tag

        logger.info(f"📡 Pushing {repository}:{tag}..")

        for line in docker_client.images.push(repository, tag=tag, stream=True, decode=True):
            if "errorDetail" in line:
                raise Exception(line["errorDetail"]["message"])
            logger.debug(line)

    if "inputs" not in step and "commands" not in step:
        return

    inputs = expand_inputs(target_rel_path, step.get("inputs", []))
    inputs_from_build = None
    from_image = previous_tag

    if "image" in step:
        # Using a different image for deploy
        from_image = step["image"]
        # Prepare command to gather the input and output of build step
        outputs = steps.get("build", {}).get("outputs", [])
        inputs_from_build = [
            (previous_tag, os.path.join(target_rel_path, o)) for o in ["."] + outputs
        ]

    dockerfile_contents = generate_dockerfile_contents(
        from_image=from_image,
        inputs=inputs,
        inputs_from_build=inputs_from_build,
        commands=step.get("commands", []),
        workdir=target_rel_path,
        pass_ssh=step.get("pass_ssh", False),
        secrets=step.get("secrets"),
        chown=step.get("chown"),
    )

    # Docker build
    logger.info(f"🚀 Deploying {target_rel_path}..")

    _, is_cached = docker_build(
        tags=compute_tags(name, "deploy"),
        dockerfile_contents=dockerfile_contents,
        dependency_paths=None,  # always run deployment
        pass_ssh=step.get("pass_ssh", False),
        secrets=step.get("secrets"),
        no_cache=no_cache,
    )
    logger.info(f"💯 Deploy finished{' (cached)' if is_cached else ''}!")


@cli.command()
@click.argument("target", default=".")
@click.pass_context
def develop(ctx, target):
    target_rel_path = os.path.relpath(target, start=ROOT_PATH)
    config = get_config(target)
    steps = config["steps"]

    # Make sure to run the previous step
    digest = ctx.invoke(prepare, target=target)

    step = steps["develop"]
    prepare_step = steps.get("prepare")
    build_step = steps["build"]
    inputs = expand_inputs(target_rel_path, build_step.get("inputs", []))
    if prepare_step:
        inputs += expand_inputs(target_rel_path, prepare_step.get("inputs", []))
    volumes = {}
    for host_path in inputs:
        volumes[os.path.abspath(os.path.join(ROOT_PATH, host_path))] = {
            "bind": f"/home/{host_path}",
            "mode": "rw",
        }
    ports = {}
    for port in step.get("ports", []):
        ports[f"{port}"] = port
    command = step.get("command")
    environment = step.get("environment")

    # Docker run
    logger.info(f"🔨 Developing {target_rel_path}..")
    docker_run(tag=digest, command=command, volumes=volumes, ports=ports, environment=environment)

    logger.info(f"👋 Finished developing {target_rel_path}")
    return digest


@cli.command()
@click.argument("target", default=".")
@click.argument("skip_previous_steps", default=False)
@click.pass_context
def prune(ctx, target, skip_previous_steps):
    if check_recursive(ctx, target, prune):
        return

    start_time = time.perf_counter()
    target_rel_path = os.path.relpath(target, start=ROOT_PATH)
    name = get_name(target_rel_path)
    for image in docker_images_list(name, last_tagged_before=arrow.utcnow().shift(days=-3)):
        # If no tag contains `master` or `latest`,
        # then this must be a branch build,
        # and it can be considered for deletion
        if any([":master" in t or ":latest" in t for t in image["tags"]]):
            logger.info(f'Skipping {image["tags"][0]}..')
            continue
        logger.info(f'Deleting {image["tags"][0]} ({round(image["size"] / 1024 / 1024)}M)..')
        docker_image_delete(image["id"], force=True)

    log_exec_time("prune", target_rel_path, start_time)


def entrypoint():
    cli()  # pylint: disable=no-value-for-parameter


if __name__ == "__main__":
    entrypoint()
