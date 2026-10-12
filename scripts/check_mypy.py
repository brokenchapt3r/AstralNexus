"""Type-check runtime, repository tests and development scripts from any cwd."""
from __future__ import annotations
import os
from pathlib import Path
from mypy import api


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    os.environ['MYPYPATH'] = str(root/'src')
    output, errors, status = api.run([
        '--python-version', '3.11', '--namespace-packages', '--no-incremental',
        '--exclude', r'(?:skills[/\\]vendor|build|dist)(?:[/\\]|$)',
        'src/AstralNexus', 'tests', 'scripts',
    ])
    print(output, end='')
    if errors:
        import sys
        print(errors, end='', file=sys.stderr)
    return status


if __name__ == '__main__':
    raise SystemExit(main())
