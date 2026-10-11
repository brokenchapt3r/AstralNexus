"""Setup script for AstralNexus."""

import re
from pathlib import Path

from setuptools import setup, find_packages
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist

# Read README file for long description
with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

identity_source = (Path(__file__).parent / "core" / "identity.py").read_text(
    encoding="utf-8"
)
version_match = re.search(r'^VERSION\s*=\s*"([^"]+)"', identity_source, re.MULTILINE)
if version_match is None:
    raise RuntimeError("core/identity.py 中缺少 VERSION")

packages = ["AstralNexus"] + [
    f"AstralNexus.{package}" for package in find_packages(where=".")
    if package != "tests" and not package.startswith("tests.")
]

# This repository maps the top-level package to '.', so automatic package
# data discovery does not reliably include these directories in wheels.
# Skills (including code templates) are assets, not importable modules.
asset_root = Path(__file__).resolve().parent
package_assets = []
for directory in ("dashboard", "skills", "contracts"):
    for asset in sorted((asset_root / directory).rglob("*")):
        relative = asset.relative_to(asset_root)
        if not asset.is_file() or any(part in {"__pycache__", ".git", "node_modules", ".venv"} for part in relative.parts):
            continue
        if asset.suffix in {".pyc", ".pyo"} or (directory == "dashboard" and asset.suffix == ".py"):
            continue
        package_assets.append(relative.as_posix())


class BuildWithTui(build_py):
    """Ship the prebuilt client without copying it into the source package."""
    def run(self):
        super().run()
        bundle = tui_bundle()
        if not bundle.is_file():
            raise RuntimeError('TUI bundle missing; run npm run build in 01_源码/tui before packaging')
        destination = Path(self.build_lib) / 'AstralNexus' / 'tui_assets' / 'entry.mjs'
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.copy_file(str(bundle), str(destination))

    def get_outputs(self, include_bytecode=1):
        return super().get_outputs(include_bytecode) + [str(Path(self.build_lib) / 'AstralNexus' / 'tui_assets' / 'entry.mjs')]


def tui_bundle():
    source = asset_root.parent / 'tui' / 'dist' / 'entry.js'
    return source if source.is_file() else asset_root / 'tui_assets' / 'entry.js'


class SourceWithTui(sdist):
    def make_release_tree(self, base_dir, files):
        super().make_release_tree(base_dir, files)
        bundle = tui_bundle()
        if not bundle.is_file(): raise RuntimeError('TUI bundle missing before source distribution')
        destination = Path(base_dir) / 'tui_assets' / 'entry.js'
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.copy_file(str(bundle), str(destination))

setup(
    name="AstralNexus",
    cmdclass={'build_py': BuildWithTui, 'sdist': SourceWithTui},
    version=version_match.group(1),
    author="AstralNexus Team",
    author_email="team@astralnexus.ai",
    description="面向超长程复杂任务的动态异构群体智能协同推理引擎",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/your-org/AstralNexus",
    packages=packages,
    package_dir={"AstralNexus": "."},
    package_data={"AstralNexus": package_assets},
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.11",
    ],
    python_requires=">=3.11",
    install_requires=[
        "numpy>=1.24.0,<2",
        "rich>=13.0.0,<14",
        "flask>=2.3.0,<3",
        "PyYAML>=6.0,<7",
        "psutil>=5.9,<8",
        "Werkzeug>=2.3.0,<3",
        "ruamel.yaml>=0.18,<0.20",
    ],
    extras_require={
        "dev": [
            "black>=22.0.0",
            "flake8>=4.0.0",
            "pytest>=7.0.0",
            "pytest-cov>=3.0.0",
            "mypy>=1.8,<2",
            "playwright>=1.40,<2",
        ],
    },
    entry_points={
        "console_scripts": [
            "astralnexus=AstralNexus.cli.main:main",
            "astralnexus-workbench=AstralNexus.dashboard.workbench_server:main",
        ],
    },
    include_package_data=True,
    zip_safe=False,
)

