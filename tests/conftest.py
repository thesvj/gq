import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).parent / "fixtures"
sys.path.insert(0, str(ROOT))


def _load_snap():
    spec = importlib.util.spec_from_file_location("gq_snap", ROOT / "gq" / "remote_files" / "snap.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def snap():
    return _load_snap()


@pytest.fixture
def fx():
    return lambda site, name: (FIX / site / name).read_text()


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("GQ_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("GQ_CACHE_DIR", str(tmp_path / "cache"))
    from gq.config import Config

    def make(text):
        p = tmp_path / "config.ini"
        p.write_text(text)
        return Config(p)
    return make
