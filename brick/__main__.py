#!/usr/bin/env python3

import glob
import logging
import tempfile
import os
import shutil
import subprocess
import sys

from braceexpand import braceexpand
import click
import docker
import yaml

docker_client = docker.from_env()

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

# Discover root path
ROOT_PATH = os.getcwd()
while not os.path.exists(os.path.join(ROOT_PATH, 'WORKSPACE')):
    ROOT_PATH = os.path.join(ROOT_PATH, '..')

# Discover git branch
GIT_BRANCH = subprocess.check_output(
    "git branch --contains `git rev-parse HEAD` | "
    "grep -v 'detached' | head -n 1 | sed 's/^* //' | "
    r"sed 's/\//\-/' | sed 's/ *//g'", shell=True, encoding='utf8').rstrip('\n')


def compute_tags(name, step_name):
    return add_version_to_tag(f'{name}_{step_name}')


def add_version_to_tag(name):
    # Last tag should be the most specific
    return [
        f'{name}:latest',
        f'{name}:{GIT_BRANCH}',
    ]


def docker_run(tag, command, volumes=None, ports=None):
    container = docker_client.containers.run(
        tag,
        command=command,
        ports=ports,
        volumes=volumes,
        remove=True,
        detach=True)
    # Attach
    try:
        for output in container.logs(stream=True, follow=True):
            sys.stdout.write(output.decode('utf8'))
    except (KeyboardInterrupt, SystemExit):
        # Quit
        container.kill()
        raise
    result = container.wait()
    if result['StatusCode']:
        exit(result['StatusCode'])


def docker_build(tags, dockerfile_contents, pass_ssh=False, no_cache=False, secrets=None):
    dockerfile_path = os.path.join(ROOT_PATH, '.brickdockerfile')
    if os.path.exists(dockerfile_path):
        logger.warn(f'{dockerfile_path} already exists at root of workspace')
        os.remove(dockerfile_path)
    with open(dockerfile_path, 'w+') as dockerfile:
        dockerfile.write(dockerfile_contents)
    try:
        iidfile = tempfile.mktemp()
        cmd = f'docker -v build . --iidfile {iidfile} -f {dockerfile_path}'
        env = {'DOCKER_BUILDKIT': '1'}
        if pass_ssh:
            cmd += ' --ssh default'
            env['SSH_AUTH_SOCK'] = os.environ['SSH_AUTH_SOCK']
        if no_cache:
            cmd += ' --no-cache'
        for k, v in (secrets or {}).items():
            src = os.path.expanduser(v["src"])
            #cmd += f' --secret id={k},src={src}'
            # For now we tar the whole secrets directory
            # as buildkit doesn't support mounting directories
            # See https://github.com/moby/buildkit/issues/970
            basename = os.path.basename(src)
            tarfile = os.path.join(ROOT_PATH, f'{basename}.tar.gz')
            subprocess.run(
                f"tar zc -C {src} --exclude='logs' . > {tarfile}",
                shell=True,
                check=True)
            cmd += f' --secret id={k},src={tarfile}'
        with subprocess.Popen(
                args=cmd,
                encoding='utf8',
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=True,
                env=env,
                universal_newlines=True,
                cwd=ROOT_PATH) as p:
            logs = [cmd]
            logger.debug(cmd)
            while True:
                line = p.stderr.readline()
                if line == "":
                    break
                log = line.rstrip('\n')
                logs += [log]
                logger.debug(log)
            returncode = p.wait()
            if returncode:
                out, err = p.communicate()
                logger.error('\n'.join(logs))
                logger.error(err)
                exit(returncode)
            os.remove(dockerfile_path)
    except (KeyboardInterrupt, SystemExit):
        os.remove(dockerfile_path)
        raise
    finally:
        # Cleanup tar files
        for k, v in (secrets or {}).items():
            src = os.path.expanduser(v["src"])
            tarfile = os.path.join(ROOT_PATH, f'{basename}.tar.gz')
            os.remove(tarfile)

    with open(iidfile) as f:
        digest = f.readline().split(":")[1].strip()
    os.remove(iidfile)

    for tag in tags:
        logger.debug(f"Tagging as {tag}..")
        repository, version = tag.split(':')
        docker_client.images.get(digest).tag(
            repository=repository,
            tag=version)

    return tags[-1]


def expand_inputs(target, inputs):
    ret = []
    for input_path in inputs:
        # Also do bash-style brace expansions before globbing
        for input_path in braceexpand(input_path):
            matches = glob.glob(os.path.join(ROOT_PATH, target, input_path), recursive=True)
            if not matches:
                logger.debug(f'Could not find an match for {os.path.join(ROOT_PATH, target, input_path)}')
                raise Exception(f'No matches found for input {input_path}')
            for g in matches:
                # Paths should be relative to root
                p = os.path.relpath(g, start=ROOT_PATH)
                ret.append(p)
    return ret


def generate_dockerfile_contents(from_image,
                                 inputs,
                                 commands,
                                 workdir,
                                 entrypoint=None,
                                 inputs_from_build=None,
                                 pass_ssh=False,
                                 secrets=None):
    dockerfile_contents = f"FROM {from_image}\n"
    if inputs_from_build:
        dockerfile_contents += '\n'.join(
            [f"COPY --from={x[0]} /home/{x[1]} /home/{x[1]}"
             for x in inputs_from_build]) + '\n'
    dockerfile_contents += '\n'.join([f"COPY {x} /home/{x}"
                                      for x in inputs]) + '\n'
    dockerfile_contents += f"WORKDIR /home/{workdir or ''}\n"
    run_flags = []
    if pass_ssh:
        run_flags += ['--mount=type=ssh']
    for k, v in (secrets or {}).items():
        # run_flags += [f'--mount=type=secret,id={k},target={v["target"]},required']
        # Use the tar file passed instead
        run_flags += [f'--mount=type=secret,id={k},target={v["target"]}.tar.gz,required']

    def generate_run_command(cmd):
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
            return f"RUN {' '.join(run_flags)} {cmd}"

    dockerfile_contents += '\n'.join([generate_run_command(cmd)
                                      for cmd in commands]) + '\n'
    # Add entrypoint
    if entrypoint:
        dockerfile_contents += f'CMD {entrypoint}'

    return dockerfile_contents


@click.group()
@click.option('--verbose', help='verbose', is_flag=True)
def cli(verbose):
    if verbose:
        handler.setLevel(logging.DEBUG)
    else:
        handler.setLevel(logging.INFO)


@cli.command()
@click.argument('target', default='.')
def prepare(target):
    target_rel_path = os.path.relpath(target, start=ROOT_PATH)
    with open(os.path.join(target, 'BUILD.yaml')) as f:
        config = yaml.load(f, Loader=yaml.FullLoader)
    steps = config['steps']
    name = config['name']

    step = steps['prepare']
    inputs = expand_inputs(target_rel_path, step['inputs'])
    dockerfile_contents = generate_dockerfile_contents(
        from_image=step['image'], inputs=inputs,
        commands=step.get('commands', []),
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
    target_rel_path = os.path.relpath(target, start=ROOT_PATH)
    with open(os.path.join(target, 'BUILD.yaml')) as f:
        config = yaml.load(f, Loader=yaml.FullLoader)
    steps = config['steps']
    name = config['name']

    # Make sure to run the previous step
    digest = ctx.invoke(prepare, target=target)

    step = steps['build']
    inputs = expand_inputs(target_rel_path, step['inputs'])
    dockerfile_contents = generate_dockerfile_contents(
        from_image=digest, inputs=inputs,
        commands=step.get('commands', []),
        entrypoint=step.get('entrypoint'),
        workdir=target_rel_path)

    # Docker build
    logger.info(f'🔨 Building {target_rel_path}..')
    digest = docker_build(
        tags=compute_tags(name, 'build') + add_version_to_tag(step.get('tag', name)),
        dockerfile_contents=dockerfile_contents)

    # Gather output
    logger.info('Collecting outputs..')
    for output in step.get('outputs', []):
        logger.debug(f'Collecting {os.path.join(target_rel_path, output)}..')
        # TODO: Use ocker container cp instead
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
    target_rel_path = os.path.relpath(target, start=ROOT_PATH)
    with open(os.path.join(target, 'BUILD.yaml')) as f:
        config = yaml.load(f, Loader=yaml.FullLoader)
    steps = config['steps']
    name = config['name']

    if 'test' not in steps:
        logger.info('Nothing to test')
        return

    # Make sure to run the previous step
    build_tag = ctx.invoke(build, target=target)

    step = steps['test']
    inputs = expand_inputs(target_rel_path, step.get('inputs', []))
    dockerfile_contents = generate_dockerfile_contents(
        from_image=build_tag, inputs=inputs,
        commands=step.get('commands', []),
        workdir=target_rel_path)

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
    target_rel_path = os.path.relpath(target, start=ROOT_PATH)
    with open(os.path.join(target, 'BUILD.yaml')) as f:
        config = yaml.load(f, Loader=yaml.FullLoader)
    steps = config['steps']
    name = config['name']

    if 'deploy' not in steps:
        logger.info('Nothing to deploy')
        return

    step = steps['deploy']

    # Make sure to run the previous step
    if 'test' in steps:
        previous_tag = ctx.invoke(test, target=target)
    elif 'build' in steps:
        previous_tag = ctx.invoke(build, target=target)
    else:
        raise Exception('Could not detect previous step')

    # Push image if needed
    if step.get('push_image') is True and steps.get('build', {}).get('tag'):
        tag = steps['build']['tag']
        logger.info(f'📡 Pushing {tag}..')
        for line in docker_client.images.push(tag, stream=True, decode=True):
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
        # Prepare command to gather the output of build
        outputs = steps.get('build', {}).get('outputs')
        if outputs:
            inputs_from_build = [(previous_tag, os.path.join(target_rel_path, o)) for o in outputs]

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
    name = config['name']

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

    # Docker run
    logger.info(f'🔨 Developping {target_rel_path}..')
    docker_run(tag=digest, command=command, volumes=volumes, ports=ports)

    logger.info(f'👋 Finished developping {target_rel_path}')
    return digest


def entrypoint():
    cli()  # pylint: disable=no-value-for-parameter


if __name__ == '__main__':
    entrypoint()
