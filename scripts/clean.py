"""Remove generated build/cache files within this checkout only."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import shutil


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    targets = [root/name for name in ('build', 'dist', '.pytest_cache', '.mypy_cache')]
    targets.extend((root/'src').glob('*.egg-info'))
    for directory in ('src', 'tests', 'scripts'):
        for parent, dirs, _ in os.walk(root/directory):
            dirs[:] = [name for name in dirs if name not in {'.git', 'node_modules'}
                       and not (Path(parent)/name).is_symlink()]
            if '__pycache__' in dirs:
                targets.append(Path(parent)/'__pycache__')
                dirs.remove('__pycache__')
    for target in targets:
        if not target.exists():
            continue
        if target.is_symlink() or not target.resolve().is_relative_to(root):
            raise ValueError('cleanup target escapes checkout: ' + str(target))
        print(target.relative_to(root))
        if not args.dry_run:
            shutil.rmtree(target)


if __name__ == '__main__':
    main()
