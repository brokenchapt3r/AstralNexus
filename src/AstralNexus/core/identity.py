from __future__ import annotations

import os
from pathlib import Path


PRODUCT_NAME = "天枢"
CODE_NAME = "AstralNexus"
VERSION = "1.0.0"
HOME_ENV = "ASTRALNEXUS_HOME"


def default_home() -> Path:
    return Path.home() / ".astralnexus"


def bootstrap_environment() -> Path:
    home_raw = os.environ.get(HOME_ENV, "").strip()
    home = Path(home_raw) if home_raw else default_home()
    os.environ[HOME_ENV] = str(home)
    os.environ.setdefault("ASTRALNEXUS_PRODUCT_NAME", PRODUCT_NAME)
    os.environ.setdefault("ASTRALNEXUS_CODE_NAME", CODE_NAME)
    return home
