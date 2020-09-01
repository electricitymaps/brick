import glob
import os

import yaml
from braceexpand import braceexpand

from .logger import logger


# Discover root path
ROOT_PATH = os.getcwd()
i = 0
while not os.path.exists(os.path.join(ROOT_PATH, 'WORKSPACE')):
    if i > 10:
        raise Exception('Maximum recursion reached. Did you launch brick from the right project?')
    ROOT_PATH = os.path.abspath(os.path.join(ROOT_PATH, os.pardir))
    i += 1


def expand_inputs(target, inputs):
    ret = []
    for input_path in inputs:
        # Also do bash-style brace expansions before globbing
        for input_path in braceexpand(input_path):
            matches = glob.glob(os.path.join(ROOT_PATH, target, input_path), recursive=True)
            if not matches:
                logger.debug(f'Could not find an match for {os.path.join(ROOT_PATH, target, input_path)}')
                raise Exception(f'No matches found for input {input_path} for target {target}')
            for g in matches:
                # Paths should be relative to root
                p = os.path.relpath(g, start=ROOT_PATH)
                ret.append(p)
    return ret


def intersecting_outputs(target, inputs):
    '''
    Detects if the inputs correspond to the output of another build
    and returns the relative paths to the build that needs to be executed first
    '''
    matches = set()
    for input_path in inputs:
        # Also do bash-style brace expansions before globbing
        for input_path in braceexpand(input_path):
            # Make relative to cwd
            input_path = os.path.abspath(os.path.join(ROOT_PATH, target, input_path))
            # Search for a BUILD.yaml
            dir_path = input_path if os.path.isdir(input_path) else os.path.dirname(input_path)
            # Check if input is a descendant of WORKSPACE
            # else there's no point searching
            if ROOT_PATH in dir_path:
                while True:
                    # Test if we have reached the current target
                    if os.path.abspath(os.path.join(ROOT_PATH, target)) == dir_path:
                        break
                    # Test if dir_path has a BUILD.yaml
                    build_path = os.path.join(dir_path, 'BUILD.yaml')
                    if os.path.exists(build_path):
                        # Open yaml and check if output matches
                        with open(build_path) as f:
                            config = yaml.load(f, Loader=yaml.FullLoader)
                            outputs = [
                                os.path.abspath(os.path.join(dir_path, x))
                                for x in config['steps'].get('build', {}).get('outputs', [])
                            ]
                            if any([x in input_path for x in outputs]):
                                matches.add(os.path.relpath(dir_path, ROOT_PATH))
                        break
                    else:
                        # This will move one level up
                        dir_path = os.path.dirname(dir_path)
                        if os.path.abspath(dir_path) in [ROOT_PATH, '/']:
                            # Abort, found nothing
                            break
    return sorted(matches)


def get_config_path(target):
    return os.path.join(target, 'BUILD.yaml')


def get_relative_config_path(target):
    return os.path.relpath(get_config_path(target), start=ROOT_PATH)


def get_config(target):
    try:
        with open(get_config_path(target)) as f:
            # TODO: we could be basic sanity checking here
            return yaml.load(f, Loader=yaml.FullLoader)
    except FileNotFoundError:
        raise Exception(f'BUILD.yaml not found.')
