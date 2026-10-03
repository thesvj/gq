"""Multi-session start: one plan, one confirmation, cap-checked as a whole, never partially over the cap."""
import pytest

from gq import cli
from gq.demo import demo_config, demo_snaps


@pytest.fixture
def fake_ssh(monkeypatch):
    calls = []

    def run(host, *args, **kw):
        calls.append((host, args))
        return 0, f"{args[1]}\n" if args[0] == "new" else "ok"
    monkeypatch.setattr(cli.remote, "run", run)
    return calls


def test_parse_specs():
    assert cli.parse_specs(["alpha:short:2", "beta:gpu"]) == [("alpha", "short", 2, {}), ("beta", "gpu", 1, {})]
    with pytest.raises(cli.GQError):
        cli.parse_specs(["alpha"])
    with pytest.raises(cli.GQError):
        cli.parse_specs(["alpha:short:x"])


def test_batch_starts_unique_names(fake_ssh):
    cfg, snaps = demo_config(), demo_snaps()
    started = cli.do_batch(cfg, [("alpha", "short", 2, {}), ("beta", "gpu", 1, {})], snaps, interactive=False)
    assert started == [("alpha", "gq-short-2"), ("alpha", "gq-short-3"), ("beta", "gq-gpu-1")]  # gq-short-1 exists
    assert [a[1][0] for a in fake_ssh] == ["new", "new", "new"]


def test_batch_refuses_over_cap_before_submitting(fake_ssh):
    cfg, snaps = demo_config(), demo_snaps()
    with pytest.raises(cli.GQError, match="only 2 slot"):           # short: cap 3, 1 used
        cli.do_batch(cfg, [("alpha", "short", 2, {}), ("alpha", "short", 1, {})], snaps, interactive=False)
    assert fake_ssh == []                                           # nothing submitted
    with pytest.raises(cli.GQError, match="only 0 slot"):
        cli.do_batch(cfg, [("alpha", "medium", 1, {})], snaps, interactive=False)


def test_batch_stops_at_first_failure(monkeypatch):
    n = {"i": 0}

    def run(host, *args, **kw):
        n["i"] += 1
        return (0, f"{args[1]}\n") if n["i"] == 1 else (3, "ERR REFUSED: cap")
    monkeypatch.setattr(cli.remote, "run", run)
    with pytest.raises(cli.GQError, match="already started: alpha:gq-short-2"):
        cli.do_batch(demo_config(), [("alpha", "short", 2, {})], demo_snaps(), interactive=False)
    assert n["i"] == 2


def test_describe_batch_total():
    plan = cli.plan_batch(demo_config(), [("alpha", "short", 2, {}), ("alpha", "long", 1, {})], demo_snaps())
    text, n = cli.describe_batch(plan)
    assert n == 3 and "TOTAL: alpha 1.2 token" in text and "× 2" in text
