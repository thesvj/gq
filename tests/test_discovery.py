"""Parsers and discovery logic of the cluster-side snapshot (gq/remote_files/snap.py)."""


def test_gres_types(snap):
    assert snap.gres_types("gpu:8(S:0-1)") == [(None, 8)]
    assert snap.gres_types("gpu:3g.71gb:16(S:0-1)") == [("3g.71gb", 16)]
    assert snap.gres_types("gpu:(null):5(IDX:0-2,4-5)") == [(None, 5)]
    assert snap.gres_types("gpu:a100:2,gpu:a40:2") == [("a100", 2), ("a40", 2)]
    assert snap.gres_types("(null)") == []


def test_tres_typed_gpu_wins(snap):
    assert snap.gpu_from_tres(snap.parse_tres("cpu=10,gres/gpu:3g.71gb=1,mem=64G")) == ("3g.71gb", 1)
    assert snap.gpu_from_tres(snap.parse_tres("cpu=16,gres/gpu=1,mem=256G")) == (None, 1)
    assert snap.gpu_from_tres(snap.parse_tres("gres/gpu=2,gres/gpu:a100=1")) == ("a100", 1)
    assert snap.gpu_from_tres({}) == (None, None)


def test_units(snap):
    assert snap.wall_secs("3-00:00:00") == 259200
    assert snap.wall_secs("06:00:00") == 21600
    assert snap.wall_secs("30") == 1800          # bare number = minutes
    assert snap.wall_secs("UNLIMITED") is None
    assert snap.mem_mb("64G") == 65536 and snap.mem_mb("65536M") == 65536 and snap.mem_mb("0") is None


def test_kv_line_values_with_spaces(snap):
    d = snap.kv_line("NodeName=x OS=Linux 5.15 #1 SMP Mon  RealMemory=10 Reason=disk full [root@2026] State=DOWN")
    assert d["OS"].startswith("Linux 5.15") and d["RealMemory"] == "10" and d["State"] == "DOWN"


def test_nodes_site_a(snap, fx):
    n = {x["node"]: x for x in snap.parse_nodes(fx("sitea", "nodes.txt"))}
    assert n["a-mig"]["gpu_type"] == "3g.71gb" and n["a-mig"]["gpu_total"] == 16 and n["a-mig"]["gpu_used"] == 1
    assert n["a-node1"]["gpu_total"] == 8 and n["a-node1"]["gpu_used"] == 5
    assert n["a-node1"]["partitions"] == ["medium", "admin"]
    assert n["a-node3"]["gpu_used"] == 8
    assert n["a-node4"]["bad"] and not n["a-node1"]["bad"]
    assert n["a-node2"]["cpu_idle"] == 10


def test_nodes_site_b_typed_and_drain(snap, fx):
    n = {x["node"]: x for x in snap.parse_nodes(fx("siteb", "nodes.txt"))}
    assert n["b-gpu1"]["gpu_type"] == "a100" and n["b-gpu1"]["gpu_used"] == 1
    assert n["b-gpu3"]["bad"] and n["b-gpu3"]["gpu_total"] == 4
    assert n["b-cpu1"]["gpu_total"] == 0


def test_account_choice(snap, fx):
    a = snap.parse_assoc(fx("sitea", "assoc.txt"))
    assert snap.choose_account(a, None) == ("lab", ["lab"], "only association")
    b = snap.parse_assoc(fx("siteb", "assoc.txt"))
    acc, cands, why = snap.choose_account(b, None)
    assert acc is None and cands == ["grpA", "grpB"] and "ambiguous" in why   # never guess
    assert snap.choose_account(b, "grpB")[0] == "grpB"                       # DefaultAccount breaks the tie
    assert snap.choose_account(b, "grpB", "grpA")[0] == "grpA"               # config wins


def test_partition_access_site_a(snap, fx):
    parts = {p["name"]: p for p in snap.parse_partitions(fx("sitea", "partitions.txt"))}
    assoc = snap.parse_assoc(fx("sitea", "assoc.txt"))
    assert snap.partition_access(parts["short"], "lab", ["lab"], assoc) == ("yes", "", "short")
    assert snap.partition_access(parts["private"], "lab", ["lab"], assoc)[0] == "no"
    assert parts["admin"]["hidden"]


def test_partition_access_site_b(snap, fx):
    parts = {p["name"]: p for p in snap.parse_partitions(fx("siteb", "partitions.txt"))}
    assoc = snap.parse_assoc(fx("siteb", "assoc.txt"))
    u, why, q = snap.partition_access(parts["gpu"], "grpA", ["users"], assoc)
    assert u == "yes" and q == "normal"          # no partition QoS: assoc default qos within AllowQos
    assert snap.partition_access(parts["staff"], "grpA", ["users"], assoc)[0] == "no"    # AllowGroups
    assert snap.partition_access(parts["old"], "grpA", ["users"], assoc)[0] == "no"      # DRAIN
    assert snap.partition_access(parts["gpu"], "banned", ["users"], assoc)[0] == "no"    # DenyAccounts
    assert snap.partition_access(parts["gpu"], None, None, assoc)[0] == "unknown"


def test_limits_from_qos_maxtres(snap, fx):
    parts = {p["name"]: p for p in snap.parse_partitions(fx("sitea", "partitions.txt"))}
    qos = snap.parse_qos(fx("sitea", "qos.txt"))
    nodes = snap.parse_nodes(fx("sitea", "nodes.txt"))
    lim = snap.partition_limits(parts["short"], qos["short"], [], nodes)
    assert (lim["cpus"], lim["mem_mb"], lim["gpu_type"], lim["gpus"], lim["time"]) == (10, 65536, "3g.71gb", 1, 21600)
    assert qos["short"]["submit_pa"] == 3 and qos["short"]["deny_on_limit"]


def test_limits_fallbacks(snap, fx):
    parts = {p["name"]: p for p in snap.parse_partitions(fx("siteb", "partitions.txt"))}
    qos = snap.parse_qos(fx("siteb", "qos.txt"))
    nodes = snap.parse_nodes(fx("siteb", "nodes.txt"))
    lim = snap.partition_limits(parts["gpu"], qos["normal"], [], nodes)   # only MaxTRESPU
    assert lim["cpus"] == 48            # capped by MaxCPUsPerNode
    assert lim["gpus"] == 2 and "MaxTRESPU" in lim["source"]
    assert lim["time"] == 172800
    lim = snap.partition_limits(parts["cpu"], None, [], nodes)            # nothing: node sizes
    assert lim["gpus"] == 0 and lim["cpus"] == 128 and lim["mem_mb"] == 250000
    assert not qos["normal"]["deny_on_limit"] and qos["normal"]["submit_pu"] == 20


def test_lua_tokens_is_parsed_not_executed(snap, fx):
    t = snap.parse_lua_tokens(fx("sitea", "job_submit.lua"))
    assert t["cost"] == {"short": 0.1, "medium": 0.5, "long": 1.0} and t["monthly"] == 36.0
    assert t["ledger"].endswith("group_tokens.json")


def _ses(name, start, part, jname=None):
    return {"name": name, "srun": {"start": start, "part": part, "name": jname}, "job": None, "match": None}


def _job(i, name, part, submit):
    return {"id": i, "name": name, "part": part, "submit": submit}


def test_match_exact_and_heuristic(snap):
    sessions = [_ses("gq-short-1", 1000, "short", "gq-short-1"), _ses("avg", 5000, "medium"),
                _ses("exp", 5001, "medium"), _ses("shell", 0, None)]
    sessions[3]["srun"] = None
    jobs = [_job("1", "gq-short-1", "short", 999), _job("2", "bash", "medium", 5001), _job("3", "bash", "medium", 5000),
            _job("4", "bash", "long", 5000)]
    out = {s["name"]: (s["job"], s["match"]) for s in snap.match_sessions(sessions, jobs)}
    assert out["gq-short-1"] == ("1", "exact")
    assert out["avg"] == ("3", "guess") and out["exp"] == ("2", "guess")   # one-to-one, nearest first
    assert out["shell"] == (None, None)


def test_match_rejects_far_and_wrong_partition(snap):
    s = snap.match_sessions([_ses("a", 1000, "short")], [_job("1", "bash", "long", 1000), _job("2", "bash", "short", 2000)])
    assert s[0]["job"] is None
