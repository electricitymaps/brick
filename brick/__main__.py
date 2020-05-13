#!/usr/bin/env python3

import glob
import logging
import tempfile
import os
import shutil
import subprocess
import sys

import arrow
import click
import docker
import yaml

from .dockerlib import docker_run, docker_build, docker_images_list, docker_image_delete
from .lib import expand_inputs, ROOT_PATH, intersecting_outputs
from .logger import logger, handler

docker_client = docker.from_env()

# Discover git branch
GIT_BRANCH = subprocess.check_output(
    "git branch --contains `git rev-parse HEAD` | "
    "grep -v 'detached' | head -n 1 | sed 's/^* //' | "
    r"sed 's/\//\-/' | sed 's/ *//g'", shell=True, encoding='utf8').rstrip('\n')

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
    install_commands = ['yarn', 'yarn install']
    # Strip flags and trim the string
    split = cmd.split('--')
    clean_command = split[0].strip()
    return clean_command in install_commands


def compute_tags(name, step_name):
    return add_version_to_tag(f'{name}_{step_name}')


def add_version_to_tag(name):
    # Last tag should be the most specific
    return [
        f'{name}:latest',
        f"{name}:{GIT_BRANCH.replace('#', '').replace('/', '-')}",
    ]


def get_name(target_rel_path):
    return target_rel_path.replace('/', '_')


def generate_dockerfile_contents(from_image,
                                 inputs,
                                 commands,
                                 workdir,
                                 entrypoint=None,
                                 inputs_from_build=None,
                                 pass_ssh=False,
                                 secrets=None,
                                 environment=None):
    dockerfile_contents = '# syntax = docker/dockerfile:experimental\n'
    dockerfile_contents += f"FROM {from_image}\n"
    if inputs_from_build:
        dockerfile_contents += '\n'.join(
            [f"COPY --from={x[0]} /home/{x[1]} /home/{x[1]}"
             for x in inputs_from_build]) + '\n'
    dockerfile_contents += '\n'.join([f'COPY ["{x}", "/home/{x}"]'
                                      for x in inputs]) + '\n'
    dockerfile_contents += f"WORKDIR /home/{workdir or ''}\n"
    run_flags = []
    if pass_ssh:
        run_flags += ['--mount=type=ssh']
    for k, v in (secrets or {}).items():
        # run_flags += [f'--mount=type=secret,id={k},target={v["target"]},required']
        # Use the tar file passed instead
        # Note: One could use --mount-type=bind if the secrets
        # were placed in the build context
        run_flags += [f'--mount=type=secret,id={k},target={v["target"]}.tar.gz,required']

    for k, v in (environment or {}).items():
        dockerfile_contents += f"ENV {k}={v}\n"

    def generate_run_command(cmd, run_flags):
        cache_version = IMAGES_TO_YARN_CACHE_VERSION_DICT.get(from_image)
        if is_yarn_install_command(cmd) and cache_version:
            location = f'{YARN_CACHE_LOCATION}/{cache_version}'
            logger.debug(f'Using yarn cache located at {location}')
            run_flags += [f'--mount=type=cache,target={location}']
        if (secrets or {}).items():
            # Wrap the run command with a tar command
            # to untar and cleanup after us
            pre = ' && '.join([
                 f'mkdir -p {v["target"]} && tar zxf {v["target"]}.tar.gz -C{v["target"]}'
                 for k, v in (secrets or {}).items()])
            post = ' && '.join([
                 f'ls  && rm -rf {v["target"]}'
                 for k, v in (secrets or {}).items()])
            return f"""RUN {' '.join(run_flags)} \
                       {pre} && \
                       {cmd} && \
                       {post}
                    """
        else:
            return f"RUN {' '.join(run_flags + [cmd])}"

    dockerfile_contents += '\n'.join([generate_run_command(cmd, run_flags)
                                      for cmd in commands]) + '\n'
    # Add entrypoint
    if entrypoint:
        dockerfile_contents += f'CMD {entrypoint}'

    return dockerfile_contents


def check_recursive(ctx, target, fun):
    if ctx.parent.params.get('recursive'):
        targets = [os.path.dirname(x) for x in sorted(glob.glob(f'{target}/**/BUILD.yaml', recursive=True))]
        logger.info(f'Found {len(targets)} target(s)..')
        for recursive_target in targets:
            # Note: the recursive parameter will not be passed
            # and thus the recursion will end here
            ctx.invoke(fun, target=recursive_target)
        return True



def skip_steps(ctx):
    if ctx.parent.params.get('skip_previous_steps'):
        logger.info(f'⚡️ Skipping previous steps!') 
        return True

def image_exists(tag):
    try:
        image = docker_client.images.get(tag)
        return True
    except docker.errors.ImageNotFound:
        return False

@click.group()
@click.option('--skip-previous-steps', help='skips previous steps', is_flag=True)
@click.option('--verbose', help='verbose', is_flag=True)
@click.option('-r', '--recursive', help='recursive', is_flag=True)
def cli(verbose, recursive, skip_previous_steps):
    if verbose:
        handler.setLevel(logging.DEBUG)
    else:
        handler.setLevel(logging.INFO)


@cli.command()
@click.argument('target', default='.')
@click.pass_context
def prepare(ctx, target):
    if check_recursive(ctx, target, prepare):
        return

    target_rel_path = os.path.relpath(target, start=ROOT_PATH)
    with open(os.path.join(target, 'BUILD.yaml')) as f:
        config = yaml.load(f, Loader=yaml.FullLoader)
    steps = config['steps']
    name = get_name(target_rel_path)

    if 'prepare' not in steps:
        logger.info('Nothing to prepare')
        return

    step = steps['prepare']
    inputs = expand_inputs(target_rel_path, step.get('inputs', []))
    dockerfile_contents = generate_dockerfile_contents(
        from_image=step['image'], inputs=inputs,
        commands=step.get('commands', []),
        environment=step.get('environment', {}),
        workdir=target_rel_path)

    # Docker build
    logger.info(f'🔨 Preparing {target_rel_path}..')
    tags = compute_tags(name, 'prepare')
    # TODO: When a PR is merged, `cache_from` will unfortunately not
    # include the branch from which we're merging
    # We're disabling it for now to see if Docker can handle caching on its own
    # Ideally cache_from would not be needed? or could tage all tags matching {name}_prepare:* ??
    digest = docker_build(
        tags=tags,
        dockerfile_contents=dockerfile_contents)
    logger.info(f'💯 Preparation phase done!')
    # TODO: For some reason, buildkit doesn't support FROM with digests
    return digest


@cli.command()
@click.argument('target', default='.')
@click.pass_context
def build(ctx, target):
    if check_recursive(ctx, target, build):
        return

    target_rel_path = os.path.relpath(target, start=ROOT_PATH)
    with open(os.path.join(target, 'BUILD.yaml')) as f:
        config = yaml.load(f, Loader=yaml.FullLoader)
    steps = config['steps']
    name = get_name(target_rel_path)

    # Make sure to run the previous step
    digest = ctx.invoke(prepare, target=target)

    step = steps['build']
    logger.info(f'🔨 Building {target_rel_path}..')

    # Build dependencies
    dependencies = intersecting_outputs(target_rel_path, step.get('inputs', []))
    if dependencies:
        logger.debug(f'Found dependencies: {dependencies}')
        for dependency in dependencies:
            logger.info(f'➡️  Building dependency {dependency}')
            ctx.invoke(build, target=os.path.join(ROOT_PATH, dependency))

    # Expand inputs (globs etc..)
    # Note dependencies must be done pre-glob
    # as else globs might return nothing (if they have not been built)
    inputs = expand_inputs(target_rel_path, step.get('inputs', []))

    # If no digest is given (because there's no build step)
    # use current image instead
    digest = digest or step['image']
    dockerfile_contents = generate_dockerfile_contents(
        from_image=digest, inputs=inputs,
        commands=step.get('commands', []),
        entrypoint=step.get('entrypoint'),
        environment=step.get('environment', {}),
        workdir=target_rel_path)

    # Docker build
    tags = compute_tags(name, 'build') + add_version_to_tag(step.get('tag', name))
    digest = docker_build(
        tags=tags,
        dockerfile_contents=dockerfile_contents)

    # Gather output
    logger.info('Collecting outputs..')
    for output in step.get('outputs', []):
        logger.debug(f'Collecting {os.path.join(target_rel_path, output)}..')
        # Make sure we check that outputs are in this folder,
        # as else the dependency system won't work
        if os.path.abspath(os.path.join(ROOT_PATH, target_rel_path)) not in os.path.abspath(os.path.join(ROOT_PATH, target_rel_path, output)):
            raise Exception(f'Output {output} is not in current folder')
        # TODO: Use docker container cp instead
        # https://docker-py.readthedocs.io/en/stable/containers.html#docker.models.containers.Container.get_archive
        host_path = os.path.join(ROOT_PATH, target_rel_path, output)
        container_path = f'/home/{os.path.join(target_rel_path, output)}'
        mounted_container_path = f'/mnt'
        if os.path.exists(host_path):
            if os.path.isdir(host_path):
                shutil.rmtree(host_path)
            else:
                os.remove(host_path)
        volumes = {}
        volumes[os.path.abspath(os.path.join(host_path, '..'))] = {
            'bind': mounted_container_path,
            'mode': 'rw'
        }
        # Verify integrity before running
        docker_client.containers.run(
            image=digest, auto_remove=True, volumes=volumes,
            command=f'mv {container_path} {mounted_container_path}')

    logger.info(f'💯 Finished building {target_rel_path}!')
    return digest


@cli.command()
@click.argument('target', default='.')
@click.pass_context
def test(ctx, target):
    if check_recursive(ctx, target, test):
        return

    target_rel_path = os.path.relpath(target, start=ROOT_PATH)
    with open(os.path.join(target, 'BUILD.yaml')) as f:
        config = yaml.load(f, Loader=yaml.FullLoader)
    steps = config['steps']
    name = get_name(target_rel_path)

    if 'test' not in steps:
        logger.info('Nothing to test')
        return

    build_tag = compute_tags(name, 'build')[-1]
    should_run_build = not skip_steps(ctx) or not image_exists(build_tag)
    if should_run_build:
        build_tag = ctx.invoke(build, target=target)

    step = steps['test']
    inputs = expand_inputs(target_rel_path, step.get('inputs', []))
    dockerfile_contents = generate_dockerfile_contents(
        from_image=build_tag, inputs=inputs,
        commands=step.get('commands', []),
        workdir=target_rel_path,
        environment=step.get('environment', {}))

    # Docker build
    logger.info(f'🔍 Testing {target_rel_path}..')
    digest = docker_build(
        tags=compute_tags(name, 'test'),
        dockerfile_contents=dockerfile_contents)
    logger.info(f'✅ Tests passed!')
    return digest


@cli.command()
@click.argument('target', default='.')
@click.pass_context
def deploy(ctx, target):
    if check_recursive(ctx, target, deploy):
        return

    target_rel_path = os.path.relpath(target, start=ROOT_PATH)
    with open(os.path.join(target, 'BUILD.yaml')) as f:
        config = yaml.load(f, Loader=yaml.FullLoader)
    steps = config['steps']
    name = get_name(target_rel_path)

    if 'deploy' not in steps:
        logger.info('Nothing to deploy')
        return

    step = steps['deploy']


    # Check if it should run previous step
    if 'test' in steps:
        previous_tag = compute_tags(name, 'test')[-1]
        should_run_test = not skip_steps(ctx) or not image_exists(previous_tag)
        if should_run_test:
            previous_tag = ctx.invoke(test, target=target)
    elif 'build' in steps:
        previous_tag = compute_tags(name, 'build')[-1]
        should_run_build = not skip_steps(ctx) or not image_exists(previous_tag)
        if should_run_build:
            previous_tag = ctx.invoke(build, target=target)
    else:
        raise Exception('Could not detect previous step')

    # Push image if needed
    if step.get('push_image') is True and steps.get('build', {}).get('tag'):
        tag = steps['build']['tag']
        logger.info(f'📡 Pushing {tag}..')
        for line in docker_client.images.push(tag, stream=True, decode=True):
            if 'errorDetail' in line:
                raise Exception(line['errorDetail']['message'])
            logger.debug(line)

    if 'inputs' not in step and 'commands' not in step:
        return

    inputs = expand_inputs(target_rel_path, step.get('inputs', []))
    dockerfile_contents = '# syntax = docker/dockerfile:experimental\n'
    inputs_from_build = None
    from_image = previous_tag

    if 'image' in step:
        # Using a different image for deploy
        from_image = step['image']
        # Prepare command to gather the input and output of build step
        outputs = steps.get('build', {}).get('outputs', [])
        inputs_from_build = [
            (previous_tag, os.path.join(target_rel_path, o))
            for o in ['.'] + outputs]

    dockerfile_contents += generate_dockerfile_contents(
        from_image=from_image, inputs=inputs,
        inputs_from_build=inputs_from_build,
        commands=step.get('commands', []),
        workdir=target_rel_path,
        pass_ssh=step.get('pass_ssh', False),
        secrets=step.get('secrets'))

    # Docker build
    logger.info(f'🚀 Deploying {target_rel_path}..')

    digest = docker_build(
        tags=compute_tags(name, 'deploy'),
        dockerfile_contents=dockerfile_contents,
        pass_ssh=step.get('pass_ssh', False),
        secrets=step.get('secrets'))
    logger.info(f'💯 Deploy finished!')


@cli.command()
@click.argument('target', default='.')
@click.pass_context
def develop(ctx, target):
    target_rel_path = os.path.relpath(target, start=ROOT_PATH)
    with open(os.path.join(target, 'BUILD.yaml')) as f:
        config = yaml.load(f, Loader=yaml.FullLoader)
    steps = config['steps']
    name = get_name(target_rel_path)

    # Make sure to run the previous step
    digest = ctx.invoke(prepare, target=target)

    step = steps['develop']
    build_step = steps['build']
    inputs = expand_inputs(target_rel_path, build_step['inputs'])
    volumes = {}
    for host_path in inputs:
        volumes[os.path.abspath(os.path.join(ROOT_PATH, host_path))] = {
            'bind': f"/home/{host_path}",
            'mode': 'rw'
        }
    ports = {}
    for port in step.get('ports', []):
        ports[f'{port}'] = port
    command = step.get('command')
    environment = step.get('environment')

    # Docker run
    logger.info(f'🔨 Developing {target_rel_path}..')
    docker_run(
        tag=digest,
        command=command,
        volumes=volumes,
        ports=ports,
        environment=environment)

    logger.info(f'👋 Finished developing {target_rel_path}')
    return digest


@cli.command()
@click.argument('target', default='.')
@click.pass_context
def prune(ctx, target):
    if check_recursive(ctx, target, prune):
        return

    target_rel_path = os.path.relpath(target, start=ROOT_PATH)
    with open(os.path.join(target, 'BUILD.yaml')) as f:
        config = yaml.load(f, Loader=yaml.FullLoader)
    steps = config['steps']
    name = get_name(target_rel_path)
    for image in docker_images_list(
        name,
        last_tagged_before=arrow.utcnow().shift(days=-3)
    ):
        # If no tag contains `master` or `latest`,
        # then this must be a branch build,
        # and it can be considered for deletion
        if any([':master' in t or ':latest' in t for t in image["tags"]]):
            logger.info(f'Skipping {image["tags"][0]}..')
            continue
        logger.info(f'Deleting {image["tags"][0]} ({round(image["size"] / 1024 / 1024)}M)..')
        docker_image_delete(image['id'], force=True)


def entrypoint():
    cli()  # pylint: disable=no-value-for-parameter


if __name__ == '__main__':
    entrypoint()
