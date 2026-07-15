#!/usr/bin/env python3
import sys
from pathlib import Path

from ailinux_kernel_builder.gui import run


def application_directory() -> Path:
    """Return the persistent data directory for source and frozen builds."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


if __name__ == "__main__":
    raise SystemExit(run(application_directory()))
