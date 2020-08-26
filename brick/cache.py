import os
import json
from pathlib import Path
from filelock import Timeout, FileLock
from typing import Dict, Optional
from typing_extensions import TypedDict  # available in Python 3.8+


HOME_PATH = str(Path.home())
CACHE_FILE = os.path.join(HOME_PATH, ".brick-cache.json")
CACHE_LOCK_FILE = f"{CACHE_FILE}.lock"
VERSION = 1
LOCK = FileLock(CACHE_LOCK_FILE)

CacheEntry = TypedDict("CacheEntry", {"dependency_hash": str})
Cache = TypedDict("Cache", {"tags": Dict[str, CacheEntry], "version": int})


def _get_cache() -> Cache:
    try:
        with open(CACHE_FILE, "r") as json_file:
            current_cache: Cache = json.load(json_file)
            return current_cache
    except FileNotFoundError:
        return Cache(tags={}, version=VERSION)


class BuildCache:
    @staticmethod
    def get_hash(tag: str) -> Optional[str]:
        with LOCK:
            cache_entry = _get_cache()["tags"].get(tag)
            return cache_entry["dependency_hash"] if cache_entry else None

    @staticmethod
    def save_hash(tag: str, dependency_hash: str) -> None:
        with LOCK:
            cache = _get_cache()
            cache["tags"][tag] = CacheEntry(dependency_hash=dependency_hash)
            with open(CACHE_FILE, "w+") as json_file:
                json_file.write(json.dumps(cache, sort_keys=True, indent=2))


__all__ = ["BuildCache"]
