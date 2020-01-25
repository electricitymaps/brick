import os
import tempfile
import subprocess
import sys

import docker

from .lib import ROOT_PATH
from .logger import logger

docker_client = docker.from_env()


def docker_run(tag, command, volumes=None, ports=None, environment=None):
    cmd = 'docker run --rm -ti'
    if volumes:
        cmd += f' {" ".join([f"-v {os.path.abspath(v)}:/home/{os.path.relpath(v, ROOT_PATH)}" for v in volumes])}'
    if ports:
        cmd += f' {" ".join([f"-p {p}:{p}" for p in ports])}'
    if ports:
        cmd += f' {" ".join([f"-e {k}={v}" for k, v in environment.items()])}'
    cmd += f' {tag} {command}'
    exit(subprocess.run(cmd, shell=True).returncode)


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
