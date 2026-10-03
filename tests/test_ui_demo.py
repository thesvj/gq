"""Headless dashboard on demo data: renders, digits attach, kill needs y."""
import asyncio

from gq import cli
from gq.tui import Dashboard


def test_dashboard_demo_attach_and_kill_abort():
    async def go():
        app = Dashboard(None, demo=True)
        async with app.run_test(size=(150, 42)) as pilot:
            await pilot.pause(0.3)
            assert len(app.rows) >= 4
            await pilot.press("k")
            await pilot.press("n")
            assert app.pending_kill is None
            await pilot.press("2")
            await pilot.pause(0.2)
        return app.result
    res = asyncio.run(go())
    assert res and res[0] in ("alpha", "beta")


def test_dashboard_survives_old_or_bad_cache(tmp_path, monkeypatch):
    import json

    from gq import tui
    from gq.config import Config
    monkeypatch.setattr(tui, "CACHE_DIR", tmp_path)
    (tmp_path / "x.json").write_text(json.dumps({"ts": 1, "tokens": 3.5, "qos": {}, "jobs": []}))      # old format
    (tmp_path / "y.json").write_text(json.dumps({"protocol": 1, "ts": 1, "tokens": 3.5, "partitions": [{}]}))  # bad
    ini = tmp_path / "c.ini"
    ini.write_text("[cluster x]\nhost = x\n[cluster y]\nhost = y\n")

    async def go():
        app = tui.Dashboard(Config(ini))
        app.action_refresh = lambda: None          # no network in tests
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(0.2)
            return list(app.snaps)
    assert asyncio.run(go()) == []


def test_gui_relaunch_uses_console_script(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.sys, "argv", ["pytest"])
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/opt/gq/bin/gq" if name == "gq" else None)
    assert cli.relaunch_argv(["ui"]) == ["/opt/gq/bin/gq", "ui"]
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    argv = cli.relaunch_argv(["ui"])
    assert argv[:3] == [cli.sys.executable, "-m", "gq"]

    script = tmp_path / "gq"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    monkeypatch.setattr(cli.sys, "argv", [str(script), "ui"])
    assert cli.relaunch_argv(["ui"]) == [str(script), "ui"]
