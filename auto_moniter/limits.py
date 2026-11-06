"""Exec wrapper applying inherited per-process POSIX resource limits."""

import os
import resource
import sys


def main():
    memory, file_size = map(int, sys.argv[1:3])
    for key, value in ((resource.RLIMIT_AS, memory), (resource.RLIMIT_FSIZE, file_size)):
        if value:
            old = resource.getrlimit(key)[1]
            bound = min(old, value) if old != resource.RLIM_INFINITY else value
            resource.setrlimit(key, (bound, bound))
    os.execvp(sys.argv[3], sys.argv[3:])


if __name__ == "__main__":
    main()
