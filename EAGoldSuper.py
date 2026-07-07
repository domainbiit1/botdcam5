#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Launcher for EAGoldSuper.

This reuses the implementation in BOTLVN.py while exposing a stable
product-facing entrypoint name:
  python3 EAGoldSuper.py
  python3 EAGoldSuper.py --worker '{"cfg":"json"}'
"""

from BOTLVN import _WORKER_MODE, _worker_load_cfg, run_worker, _gui_main


if __name__ == "__main__":
    if _WORKER_MODE:
        run_worker(_worker_load_cfg())
    else:
        _gui_main()

