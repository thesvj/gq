"""Values that reach remote command lines must be validated; ssh option injection must be impossible."""
import pytest

from gq import remote
from gq.config import ConfigError, safe


@pytest.mark.parametrize("bad", ["a;b", "$(id)", "`id`", "a b", "x\ny", "-oProxyCommand=sh", "", "a|b", "a&b", "'q'"])
def test_safe_rejects(bad):
    with pytest.raises(ConfigError):
        safe(bad)


@pytest.mark.parametrize("good", ["gpu:3g.71gb:1", "1-00:00:00", "64G", "~/proj/x", "node[1-3]".replace("[", "").replace("]", "")])
def test_safe_accepts(good):
    assert safe(good) == good


def test_bad_host_rejected(cfg):
    with pytest.raises(ConfigError):
        cfg("[cluster x]\nhost = -oProxyCommand=evil\n")
    with pytest.raises(ConfigError):
        cfg("[cluster x]\nhost = a;b\n")
    with pytest.raises(ConfigError):
        cfg("[cluster x]\ntokens = cmd:relative/path\n")
    with pytest.raises(ConfigError):
        cfg("[cluster x]\ndefault.short = cpus=$(id)\n")


def test_run_validates_and_quotes(monkeypatch):
    seen = {}

    def fake(cmd, **kw):
        seen["cmd"] = cmd

        class P:
            returncode, stdout, stderr = 0, "ok", ""
        return P()
    monkeypatch.setattr(remote.subprocess, "run", fake)
    remote.run("host", "jobid", "gq-short-1")
    assert seen["cmd"][-2:] == ["host", "~/.gq/current/gqr jobid gq-short-1"]
    assert "--" in seen["cmd"]
    with pytest.raises(ConfigError):
        remote.run("host", "jobid", "x;rm -rf ~")


def test_attach_argv_falls_back_term_and_quotes():
    argv = remote.attach_argv("h", "gq-short-1")
    assert argv[-2] == "h" and "infocmp" in argv[-1] and "xterm-256color" in argv[-1] and "tmux attach -t =gq-short-1" in argv[-1]
    with pytest.raises(ConfigError):
        remote.attach_argv("h", "x;y")


def test_forward_only_deploy(monkeypatch):
    monkeypatch.setattr(remote, "__version__", "0.3.0")
    assert remote.needs_deploy(None)
    assert remote.needs_deploy("0.2.9+abc")
    assert remote.needs_deploy("0.3.0+otherhash")
    assert not remote.needs_deploy("0.4.0+abc")          # never downgrade a newer cluster install
    assert remote.release_id() == remote.release_id()


def test_newer_remote_is_not_downgraded(monkeypatch):
    """A newer cluster install with a different protocol must not be overwritten."""
    import json

    monkeypatch.setattr(remote, "__version__", "0.3.0")
    deployed = []
    monkeypatch.setattr(remote, "deploy", lambda *a, **k: deployed.append(1) or (True, "deployed"))
    body = json.dumps({"protocol": 9, "gq_version": "0.9.0+newer", "ts": 1})
    monkeypatch.setattr(remote, "run", lambda *a, **k: (0, body))

    class C:
        host, account, tokens, name = "h", "auto", "none", "x"

    class G:
        agents = False

    snap, err = remote.snapshot(C(), G())
    assert deployed == []
    assert err is None and snap["protocol"] == 9 and snap["cluster"] == "x"


def test_protocol_constants_match():
    import re
    from pathlib import Path
    src = (Path(remote.__file__).parent / "remote_files" / "snap.py").read_text()
    assert int(re.search(r"^PROTOCOL = (\d+)", src, re.M).group(1)) == remote.PROTOCOL
