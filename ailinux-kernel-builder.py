#!/usr/bin/env python3
from pathlib import Path

from ailinux_kernel_builder.gui import run


if __name__ == "__main__":
    raise SystemExit(run(Path(__file__).resolve().parent))
