"""Exec gate for worker processes.

The supervisor starts `python -m lupus.gate <argv…>`, durably records the gate's pid as the
run's process-group leader, and only then sends the go byte. If the supervisor dies first the
gate reads EOF and exits without ever executing the worker, so there is never a writer the
database does not know about.

For an interactive session the go byte arrives on a separate descriptor (LUPUS_GATE_FD) and
standard input stays the user's terminal.
"""

import os
import sys


def main() -> None:
    fd = os.environ.pop("LUPUS_GATE_FD", None)
    if fd is not None:
        go = os.read(int(fd), 1)
        os.close(int(fd))
    else:
        go = sys.stdin.buffer.read(1)
    if go != b"g" or len(sys.argv) < 2:
        os._exit(111)
    if fd is None:
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, 0)
        os.close(devnull)
    try:
        os.execvp(sys.argv[1], sys.argv[1:])
    except OSError:
        os._exit(127)


if __name__ == "__main__":
    main()
