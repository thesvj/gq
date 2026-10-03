from gq import model
from gq.demo import demo_config, demo_snaps


def test_request_policy_and_overrides(cfg):
    c = cfg("[gq]\npolicy = half\n[cluster alpha]\nhost = alpha\ndefault.long = cpus=4 time=8:00:00\n").clusters["alpha"]
    s = demo_snaps()["alpha"]
    r = model.request(c, s, "medium")
    assert r == {"cpus": "8", "mem": "128G", "time": "1-00:00:00", "gres": "gpu:1", "node": None}
    assert model.request(c, s, "short")["gres"] == "gpu:3g.71gb:1"
    r = model.request(c, s, "long")
    assert r["cpus"] == "4" and r["time"] == "8:00:00"


def test_max_policy():
    c, s = demo_config().clusters["alpha"], demo_snaps()["alpha"]
    assert model.request(c, s, "long")["mem"] == "512G"


def test_part_status_full_and_cost():
    c, s = demo_config().clusters["alpha"], demo_snaps()["alpha"]
    st = {p["name"]: model.part_status(c, s, p) for p in s["partitions"]}
    assert st["medium"]["full"] and st["medium"]["full_why"] == "account cap"
    assert not st["short"]["full"] and st["short"]["usable_gpus"] == 11
    assert st["long"]["cost"] == 1.0
    b = demo_config().clusters["beta"]
    sb = demo_snaps()["beta"]
    gpu = model.part_status(b, sb, model.partition(sb, "gpu"))
    assert gpu["free"] == 3 and gpu["cost"] is None       # drained node excluded
    assert not model.part_status(b, sb, model.partition(sb, "cpu"))["gpu_partition"]


def test_session_rows_order():
    rows = model.session_rows(["alpha", "beta"], demo_snaps())
    kinds = [r["kind"] for r in rows]
    assert kinds == sorted(kinds, key={"job": 0, "orphan": 1, "shell": 2}.get)
    assert any(r["kind"] == "orphan" and r["job"]["state"] == "PENDING" for r in rows)


def test_formatting():
    assert model.fmt_wall(259200) == "3-00:00:00" and model.fmt_wall(21600) == "6:00:00"
    assert model.fmt_mem(65536) == "64G" and model.human(90061) == "1d01h"
