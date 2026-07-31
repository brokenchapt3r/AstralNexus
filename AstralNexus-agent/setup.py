"""Setup script for AstralNexus."""

import re
from pathlib import Path

from setuptools import find_namespace_packages, setup

# Read README file for long description
with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

identity_source = (Path(__file__).parent / "core" / "identity.py").read_text(
    encoding="utf-8"
)
version_match = re.search(r'^VERSION\s*=\s*"([^"]+)"', identity_source, re.MULTILINE)
if version_match is None:
    raise RuntimeError("core/identity.py 中缺少 VERSION")

discovered_packages = find_namespace_packages(
    where=".",
    exclude=["build", "build.*", "tests", "tests.*", "*.egg-info", "*.egg-info.*"],
)
packages = ["AstralNexus"] + [
    f"AstralNexus.{package}"
    for package in discovered_packages
    if all(part.isidentifier() for part in package.split("."))
]

setup(
    name="AstralNexus",
    version=version_match.group(1),
    author="AstralNexus Team",
    author_email="team@astralnexus.ai",
    description="面向超长程复杂任务的动态异构群体智能协同推理引擎",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/your-org/AstralNexus",
    packages=packages,
    package_dir={"AstralNexus": "."},
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
    ],
    python_requires=">=3.8",
    install_requires=[
        "numpy>=1.24.0,<2",
        "rich>=13.0.0,<14",
        "flask>=2.3.0,<3",
    ],
    extras_require={
        "dev": [
            "black>=22.0.0",
            "flake8>=4.0.0",
            "pytest>=7.0.0",
            "pytest-cov>=3.0.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "astralnexus=AstralNexus.cli.main:main",
        ],
    },
    include_package_data=True,
    zip_safe=False,
)
