"""Forwarder. This directory is named gq and often sits in $HOME.

`python -m gq` (what Super+G used) puts cwd on sys.path. Python then treats
~/gq as a namespace package, so the real package in gq/gq never gets __version__.
This file makes the directory a normal package and loads the inner one.
"""
import importlib.util
import sys
from pathlib import Path

_inner = Path(__file__).resolve().parent / "gq"
_spec = importlib.util.spec_from_file_location(
    __name__,
    _inner / "__init__.py",
    submodule_search_locations=[str(_inner)],
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[__name__] = _mod
_spec.loader.exec_module(_mod)
