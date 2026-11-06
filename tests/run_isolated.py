"""Run the suite with a minimal environment and loopback-only Python sockets.

Usage: python tests/run_isolated.py [pytest arguments]
       python tests/run_isolated.py --installed [pytest arguments]

The Python-level socket guard also loads in Python child processes through
PYTHONPATH. It is not an operating-system network namespace.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    args = sys.argv[1:]
    installed = bool(args and args[0] == '--installed')
    if installed:
        args = args[1:]
    state = root / '.scratch' / 'tests' / 'runner'
    home = state / 'home'
    temp = state / 'tmp'
    home.mkdir(parents=True, exist_ok=True)
    temp.mkdir(parents=True, exist_ok=True)
    paths = [str(root / 'tests' / 'isolation')]
    if not installed:
        paths.append(str(root / 'src'))
    env = {
        'HOME': str(home),
        'PATH': os.pathsep.join((str(Path(sys.executable).parent), '/usr/bin', '/bin')),
        'PYTHONPATH': os.pathsep.join(paths),
        'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1',
        'PYTHONDONTWRITEBYTECODE': '1',
        'TMPDIR': str(temp),
    }
    config = ['-c', os.devnull] if installed else []
    return subprocess.run([sys.executable, '-m', 'pytest', *config, *(args or ['-q'])],
                          cwd=root, env=env, check=False).returncode


if __name__ == '__main__':
    raise SystemExit(main())
