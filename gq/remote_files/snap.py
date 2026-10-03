#!/usr/bin/env python3
"""gq snapshot + discovery. Runs on the cluster LOGIN node, read-only.

It only *queries* Slurm (scontrol/sacctmgr/squeue), tmux and ps, and reads files under ~/.gq.
It never submits, cancels or computes. Output: one JSON document on stdout.

Must stay compatible with Python 3.6 (old cluster login nodes): no dataclasses, no walrus,
no capture_output/text= in subprocess, no fromisoformat.
"""
import argparse
import glob
import grp
import json
import os
import re
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

PROTOCOL = 1  # bump when the JSON shape changes incompatibly
HOME = os.path.expanduser("~")
GQ = os.path.join(HOME, ".gq")
QDIR = os.path.join(GQ, "q")
CACHE = os.path.join(GQ, "cache")
SAFE = re.compile(r"^[A-Za-z0-9_.:,=@/+%-]*$")
ERRORS = []


# --------------------------------------------------------------------------- helpers
def sh(args, timeout=15):
    """Run a read-only command; never raises. Returns stdout ('' on failure)."""
    try:
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        if p.returncode != 0:
            err = p.stderr.decode("utf-8", "replace").strip().splitlines()
            ERRORS.append("%s: rc=%d %s" % (args[0], p.returncode, (err[-1] if err else "")[:160]))
        return p.stdout.decode("utf-8", "replace")
    except Exception as e:  # missing binary, timeout
        ERRORS.append("%s: %s" % (args[0], type(e).__name__))
        return ""


def to_ts(s):
    try:
        return int(datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").timestamp())
    except Exception:
        return 0


def wall_secs(s):
    """Slurm time 'D-HH:MM:SS' / 'HH:MM:SS' / 'MM:SS' / 'MM' -> seconds (None if unlimited/blank)."""
    if not s or s.upper() in ("UNLIMITED", "INFINITE", "INVALID", "NOT_SET", "N/A", "NONE"):
        return None
    d = 0
    if "-" in s:
        d, s = s.split("-", 1)
    try:
        p = [int(x) for x in s.split(":")]
    except ValueError:
        return None
    if len(p) == 1:  # bare number = minutes in slurm
        return int(d) * 86400 + p[0] * 60
    while len(p) < 3:
        p.insert(0, 0)
    return int(d) * 86400 + p[0] * 3600 + p[1] * 60 + p[2]


def mem_mb(s):
    """'64G' / '65536M' / '65536' (MB) / '2063933M' -> MB int; None if blank/0/unlimited."""
    if s is None:
        return None
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([KMGTP]?)", str(s).upper())
    if not m:
        return None
    v = float(m.group(1)) * {"": 1, "K": 1.0 / 1024, "M": 1, "G": 1024, "T": 1024 ** 2, "P": 1024 ** 3}[m.group(2)]
    return int(v) or None


def kv_line(line):
    """Parse one `scontrol show X -o` line into a dict. Values may contain spaces (OS=, Reason=)."""
    out = {}
    for part in re.split(r"\s+(?=[A-Za-z][A-Za-z0-9_/:.]*=)", line.strip()):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = v.strip()
    return out


def parse_tres(s):
    """'cpu=10,gres/gpu:3g.71gb=1,mem=64G' -> {'cpu': '10', 'gres/gpu:3g.71gb': '1', 'mem': '64G'}"""
    out = {}
    for item in (s or "").split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def gpu_from_tres(t):
    """TRES dict -> (gpu_type or None, count or None). A typed key (gres/gpu:TYPE) wins over untyped."""
    typed = [(k.split(":", 1)[1], v) for k, v in t.items() if k.startswith("gres/gpu:")]
    for typ, v in typed:
        if v.isdigit() and int(v) > 0:
            return typ, int(v)
    v = t.get("gres/gpu")
    if v and v.isdigit():
        return None, int(v)
    return None, None


def gres_types(s):
    """Node Gres= string 'gpu:H100:2(S:0),gpu:A100:1' / 'gpu:8(S:0-1)' -> [(type|None, n)]."""
    out = []
    s = re.sub(r"\((?!null\))[^)]*\)", "", (s or "").replace("(null)", "null"))
    for item in s.split(","):
        f = item.strip().split(":")
        if not f or f[0] != "gpu":
            continue
        if len(f) == 2 and f[1].isdigit():
            out.append((None, int(f[1])))
        elif len(f) >= 3 and f[-1].isdigit():
            t = ":".join(f[1:-1])
            out.append((None if t in ("null", "") else t, int(f[-1])))
    return out


# --------------------------------------------------------------------------- discovery parsers (pure)
def parse_partitions(text):
    parts = []
    for line in text.splitlines():
        if not line.startswith("PartitionName="):
            continue
        d = kv_line(line)
        parts.append({
            "name": d.get("PartitionName"),
            "state": d.get("State", "UP"),
            "qos": None if d.get("QoS") in (None, "N/A", "") else d.get("QoS"),
            "allow_qos": [] if d.get("AllowQos", "ALL") == "ALL" else d.get("AllowQos", "").split(","),
            "deny_qos": [x for x in d.get("DenyQos", "").split(",") if x and x != "(null)"],
            "allow_accounts": None if d.get("AllowAccounts", "ALL") == "ALL" else d.get("AllowAccounts", "").split(","),
            "deny_accounts": [x for x in d.get("DenyAccounts", "").split(",") if x and x != "(null)"],
            "allow_groups": None if d.get("AllowGroups", "ALL") == "ALL" else d.get("AllowGroups", "").split(","),
            "hidden": d.get("Hidden", "NO") == "YES",
            "max_time": wall_secs(d.get("MaxTime")),
            "default_time": wall_secs(d.get("DefaultTime")),
            "max_mem_per_node": mem_mb(d.get("MaxMemPerNode")) if d.get("MaxMemPerNode", "UNLIMITED") != "UNLIMITED" else None,
            "max_cpus_per_node": int(d["MaxCPUsPerNode"]) if d.get("MaxCPUsPerNode", "").isdigit() else None,
            "tres": parse_tres(d.get("TRES")),
            "nodes": d.get("Nodes", ""),
        })
    return parts


def parse_nodes(text):
    nodes = []
    for line in text.splitlines():
        if not line.startswith("NodeName="):
            continue
        d = kv_line(line)
        cfg, alloc = parse_tres(d.get("CfgTRES")), parse_tres(d.get("AllocTRES"))
        types = gres_types(d.get("Gres"))
        total = sum(n for _, n in types) or (int(cfg["gres/gpu"]) if cfg.get("gres/gpu", "").isdigit() else 0)
        used = alloc.get("gres/gpu")
        if used is None:  # sum typed allocations
            used = sum(int(v) for k, v in alloc.items() if k.startswith("gres/gpu:") and v.isdigit())
        used = int(used) if str(used).isdigit() else 0
        state = d.get("State", "")
        cpu_tot = int(d.get("CPUEfctv") or d.get("CPUTot") or 0)
        real, amem = mem_mb(d.get("RealMemory")) or 0, mem_mb(d.get("AllocMem")) or 0
        nodes.append({
            "node": d.get("NodeName"),
            "partitions": [p for p in d.get("Partitions", "").split(",") if p],
            "state": state,
            "bad": bool(re.search(r"DOWN|DRAIN|FAIL|MAINT|NOT_RESPONDING|INVAL|POWER(ED|ING)_DOWN|FUTURE|UNKNOWN|REBOOT", state)),
            "gpu_type": next((t for t, _ in types if t), None),
            "gpu_total": total,
            "gpu_used": min(used, total) if total else used,
            "cpu_total": cpu_tot,
            "cpu_idle": max(0, cpu_tot - int(d.get("CPUAlloc") or 0)),
            "mem_total_mb": real,
            "mem_free_mb": max(0, real - amem),
        })
    return nodes


def parse_qos(text):
    """sacctmgr -nP show qos format=name,maxsubmitpa,maxjobspa,maxsubmitpu,maxjobspu,maxtres,maxtrespu,maxwall,flags"""
    out = {}
    for line in text.splitlines():
        f = line.split("|")
        if len(f) < 9 or not f[0]:
            continue
        n = lambda s: int(s) if s.isdigit() else None  # noqa: E731
        out[f[0]] = {"submit_pa": n(f[1]), "jobs_pa": n(f[2]), "submit_pu": n(f[3]), "jobs_pu": n(f[4]),
                     "max_tres": parse_tres(f[5]), "max_tres_pu": parse_tres(f[6]),
                     "max_wall": wall_secs(f[7]), "deny_on_limit": "DenyOnLimit" in f[8]}
    return out


def parse_assoc(text):
    """sacctmgr -nP show assoc user=$USER format=account,partition,qos,defaultqos,maxtres,grptres"""
    rows = []
    for line in text.splitlines():
        f = (line.split("|") + [""] * 6)[:6]
        if not f[0]:
            continue
        rows.append({"account": f[0], "partition": f[1] or None, "qos": [q for q in f[2].split(",") if q],
                     "default_qos": f[3] or None, "max_tres": parse_tres(f[4]), "grp_tres": parse_tres(f[5])})
    return rows


def choose_account(assoc, default_account, override=None):
    """-> (account or None, candidates, reason). Never guesses between several accounts."""
    cands = sorted({r["account"] for r in assoc})
    if override and override != "auto":
        return override, cands, "config"
    if len(cands) == 1:
        return cands[0], cands, "only association"
    if default_account and default_account in cands:
        return default_account, cands, "slurm DefaultAccount"
    if not cands and default_account:
        return default_account, [default_account], "slurm DefaultAccount (no accounting rows)"
    return None, cands, "ambiguous: set account= in config" if cands else "unknown"


def partition_access(p, account, groups, assoc):
    """-> (usable 'yes'|'no'|'unknown', why, qos_to_use)"""
    if p["state"] not in ("UP",):
        return "no", "partition %s" % p["state"].lower(), None
    if account and p["allow_accounts"] is not None and account not in p["allow_accounts"]:
        return "no", "account not in AllowAccounts", None
    if account and account in p["deny_accounts"]:
        return "no", "account in DenyAccounts", None
    if p["allow_groups"] is not None and groups is not None and not (set(p["allow_groups"]) & set(groups)):
        return "no", "not in AllowGroups", None
    rows = [r for r in assoc if r["account"] == account and r["partition"] in (None, p["name"])] if account else []
    my_qos = sorted({q for r in rows for q in r["qos"]})
    default_qos = next((r["default_qos"] for r in rows if r["default_qos"]), None)
    if p["qos"]:
        qos = p["qos"]  # partition QoS is applied automatically; -q only needed if site requires it
        if p["allow_qos"] and qos not in p["allow_qos"]:
            qos = None
    else:
        allowed = [q for q in (p["allow_qos"] or my_qos) if q not in p["deny_qos"]]
        cand = [q for q in allowed if not my_qos or q in my_qos]
        qos = default_qos if default_qos in cand else (cand[0] if len(cand) == 1 else None)
    if p["allow_qos"] and my_qos and not (set(p["allow_qos"]) & set(my_qos)):
        return "no", "none of your QoS is in AllowQos", qos
    if account is None:
        return "unknown", "account unknown", qos
    if assoc and not rows:
        return "unknown", "no association row for this partition", qos
    return "yes", "", qos


def partition_limits(p, qos, assoc_rows, nodes):
    """Max request for ONE job: dict(cpus, mem_mb, gpu_type, gpus, time, source)."""
    src = []
    t = {}
    if qos and qos.get("max_tres"):
        t, src = dict(qos["max_tres"]), ["qos MaxTRES"]
    elif qos and qos.get("max_tres_pu"):
        t, src = dict(qos["max_tres_pu"]), ["qos MaxTRESPU"]
    else:
        for r in assoc_rows:
            if r["max_tres"]:
                t, src = dict(r["max_tres"]), ["assoc MaxTRES"]
                break
    pnodes = [n for n in nodes if p["name"] in n["partitions"] and not n["bad"]] or \
             [n for n in nodes if p["name"] in n["partitions"]]
    cpus = int(t["cpu"]) if t.get("cpu", "").isdigit() else None
    mem = mem_mb(t.get("mem"))
    gtype, gpus = gpu_from_tres(t)
    if pnodes:  # never exceed the smallest node that could run it
        gpu_nodes = [n for n in pnodes if n["gpu_total"]]
        base = gpu_nodes or pnodes
        node_cpu = min(n["cpu_total"] for n in base)
        node_mem = min(n["mem_total_mb"] for n in base)
        if p.get("max_cpus_per_node"):
            node_cpu = min(node_cpu, p["max_cpus_per_node"])
        if p.get("max_mem_per_node"):
            node_mem = min(node_mem, p["max_mem_per_node"])
        if cpus is None or cpus > node_cpu:
            cpus, src = node_cpu, src + ["node CPUs"]
        if mem is None or mem > node_mem:
            mem, src = node_mem, src + ["node memory"]
        if gpus is None and gpu_nodes:
            gpus = 1  # unlimited by policy -> start with 1 GPU, user can raise it
            src.append("1 GPU default")
        if gtype is None and gpu_nodes:
            ts = {n["gpu_type"] for n in gpu_nodes}
            if len(ts) == 1 and next(iter(ts)) and ":" not in "".join(ts):
                gtype = None  # untyped request works on a single-type partition
    wall = (qos or {}).get("max_wall") or p.get("max_time")
    return {"cpus": cpus or 1, "mem_mb": mem or 1024, "gpu_type": gtype, "gpus": gpus or 0,
            "time": wall, "source": ", ".join(src) or "defaults"}


def parse_lua_tokens(text):
    """Bundled token provider for sites whose job_submit.lua holds `TOKEN_COST = { part = n }`.
    Data is read with regexes only; the Lua is never executed."""
    out = {"cost": {}, "monthly": None, "ledger": None}
    m = re.search(r"TOKEN_COST\s*=\s*\{(.*?)\}", text, re.S)
    if m:
        for k, v in re.findall(r"([A-Za-z0-9_]+)\s*=\s*([0-9.]+)", m.group(1)):
            out["cost"][k] = float(v)
    m = re.search(r"DEFAULT_TOKENS\s*=\s*([0-9.]+)", text)
    if m:
        out["monthly"] = float(m.group(1))
    m = re.search(r"token_file\s*=\s*\"([^\"]+)\"", text)
    if m:
        out["ledger"] = m.group(1)
    return out


def match_sessions(sessions, jobs):
    """Map tmux sessions to jobs. Exact: srun -J name / gq-* session == job name.
    Heuristic (hand-started): same partition and |srun start - job submit| <= 300 s, one-to-one."""
    taken = set()
    for s in sessions:
        want = (s.get("srun") or {}).get("name") or (s["name"] if s["name"].startswith("gq-") else None)
        for j in jobs:
            if want and j["name"] == want and j["id"] not in taken:
                s["job"], s["match"] = j["id"], "exact"
                taken.add(j["id"])
                break
    pairs = []
    for s in sessions:
        if s.get("job") or not s.get("srun"):
            continue
        for j in jobs:
            if j["id"] in taken or (s["srun"].get("part") and s["srun"]["part"] != j["part"]):
                continue
            d = abs(s["srun"]["start"] - j["submit"])
            if d <= 300:
                pairs.append((d, s["name"], j["id"]))
    by_name = {s["name"]: s for s in sessions}
    used = set()
    for d, sn, jid in sorted(pairs):
        if sn in used or jid in taken:
            continue
        by_name[sn]["job"], by_name[sn]["match"] = jid, "guess"
        used.add(sn)
        taken.add(jid)
    return sessions


# --------------------------------------------------------------------------- live collectors
def collect_sessions():
    procs, children = {}, {}
    for line in sh(["ps", "-u", str(os.getuid()), "-o", "pid=,ppid=,lstart=,args="]).splitlines():
        f = line.split()
        if len(f) < 8:
            continue
        try:
            st = int(datetime.strptime(" ".join(f[2:7]), "%a %b %d %H:%M:%S %Y").timestamp())
        except Exception:
            st = 0
        procs[int(f[0])] = (int(f[1]), st, " ".join(f[7:]))
    for pid, (pp, _, _) in procs.items():
        children.setdefault(pp, []).append(pid)

    def find_srun(pid, depth=0):
        for c in sorted(children.get(pid, [])):
            if os.path.basename(procs[c][2].split()[0]) == "srun":
                return c
            if depth < 3:
                r = find_srun(c, depth + 1)
                if r:
                    return r
        return None

    def arg(a, short, long_):
        m = re.search(r"(?:^|\s)(?:%s\s*|%s[=\s]+)(\S+)" % (re.escape(short), re.escape(long_)), a)
        return m.group(1) if m else None

    sessions = {}
    fmt = "#{session_name}|#{pane_pid}|#{pane_dead}|#{pane_current_command}|#{session_attached}|#{session_created}"
    for line in sh(["tmux", "list-panes", "-a", "-F", fmt]).splitlines():
        f = line.split("|")
        if len(f) < 6 or not f[1].isdigit():
            continue
        s = sessions.setdefault(f[0], {"name": f[0], "dead": False, "fg": f[3], "attached": int(f[4] or 0),
                                       "created": int(f[5] or 0), "srun": None, "job": None, "match": None})
        s["dead"] = s["dead"] or f[2] == "1"
        if s["srun"] is None:
            sp = find_srun(int(f[1]))
            if sp:
                a = procs[sp][2]
                s["srun"] = {"start": procs[sp][1], "part": arg(a, "-p", "--partition"),
                             "name": arg(a, "-J", "--job-name"), "args": a[:400]}
    return sorted(sessions.values(), key=lambda s: s["name"])


def collect_jobs():
    jobs = []
    for line in sh(["squeue", "-h", "-u", str(os.getuid()), "-o", "%i|%j|%P|%T|%N|%V|%S|%L|%l|%r|%b|%C|%m|%q|%a"]).splitlines():
        f = line.split("|")
        if len(f) < 15:
            continue
        jobs.append({"id": f[0], "name": f[1], "part": f[2], "state": f[3], "node": f[4] or None,
                     "submit": to_ts(f[5]), "start": to_ts(f[6]), "left": wall_secs(f[7]), "limit": wall_secs(f[8]),
                     "reason": f[9], "gres": f[10], "cpus": f[11], "mem": f[12], "qos": f[13], "account": f[14]})
    return jobs


def collect_agents(jobs):
    now = time.time()
    agents, runs = {}, []
    for j in jobs:
        d = os.path.join(QDIR, j["id"])
        if not os.path.isdir(d):
            continue
        try:
            os.listdir(d)  # refresh NFS attribute cache
        except OSError:
            continue
        a = {"alive": None, "util": None, "gmem": None, "idle_min": None}
        try:
            a["alive"] = int(now - int(open(os.path.join(d, "alive")).read().split()[1]))
        except Exception:
            pass
        try:
            g = open(os.path.join(d, "gpu")).read().split()
            a["util"], a["gmem"], a["idle_min"] = g[1], g[2], int(g[3])
        except Exception:
            pass
        agents[j["id"]] = a
        for f in glob.glob(os.path.join(d, "*.start")) + glob.glob(os.path.join(d, "*.sh")):
            rid = os.path.basename(f).rsplit(".", 1)[0]
            rc = None
            try:
                rc = open(os.path.join(d, rid + ".rc")).read().strip()
            except Exception:
                pass
            st = "queued" if f.endswith(".sh") else ("done" if rc is not None else "running")
            runs.append({"job": j["id"], "id": rid, "status": st, "rc": rc, "t": int(os.path.getmtime(f))})
    runs.sort(key=lambda r: -r["t"])
    return agents, runs[:40]


def slurm_conf_dir():
    c = os.environ.get("SLURM_CONF")
    if not c:
        m = re.search(r"^SLURM_CONF\s*=\s*(\S+)", sh(["scontrol", "show", "config"]), re.M)
        c = m.group(1) if m else "/etc/slurm/slurm.conf"
    return os.path.dirname(c)


def tokens(provider, account):
    """Token providers: none | lua_job_submit | cmd:/abs/path (prints {"balance","monthly","cost":{}})."""
    if not provider or provider == "none":
        return None
    try:
        if provider == "lua_job_submit":
            info = parse_lua_tokens(open(os.path.join(slurm_conf_dir(), "job_submit.lua")).read())
            bal = None
            if info["ledger"] and account:
                bal = json.load(open(info["ledger"])).get(account, {}).get("tokens_remaining")
            return {"balance": bal, "monthly": info["monthly"], "cost": info["cost"], "provider": provider}
        if provider.startswith("cmd:"):
            path = provider[4:]
            if not path.startswith("/") or not SAFE.match(path):
                raise ValueError("token cmd must be an absolute safe path")
            d = json.loads(sh([path, account or ""], timeout=10) or "{}")
            d["provider"] = provider
            return d
        raise ValueError("unknown token provider")
    except Exception as e:
        ERRORS.append("tokens(%s): %s" % (provider, type(e).__name__))
        return {"balance": None, "monthly": None, "cost": {}, "provider": provider, "error": True}


# --------------------------------------------------------------------------- main
def discover(account_override, cache_s=600):
    key = re.sub(r"[^A-Za-z0-9_-]", "_", account_override or "auto")
    path = os.path.join(CACHE, "discovery-%s.json" % key)
    try:
        if time.time() - os.path.getmtime(path) < cache_s:
            return json.load(open(path))
    except Exception:
        pass
    me = os.environ.get("USER") or str(os.getuid())
    with ThreadPoolExecutor(4) as ex:
        f_part = ex.submit(sh, ["scontrol", "show", "partition", "-o"])
        f_qos = ex.submit(sh, ["sacctmgr", "-nP", "show", "qos",
                               "format=name,maxsubmitpa,maxjobspa,maxsubmitpu,maxjobspu,maxtres,maxtrespu,maxwall,flags"])
        f_assoc = ex.submit(sh, ["sacctmgr", "-nP", "show", "assoc", "user=" + me,
                                 "format=account,partition,qos,defaultqos,maxtres,grptres"])
        f_user = ex.submit(sh, ["sacctmgr", "-nP", "show", "user", me, "format=defaultaccount"])
        f_ver = ex.submit(sh, ["scontrol", "--version"])
    qos_text, assoc_text = f_qos.result(), f_assoc.result()
    mode = "full" if qos_text.strip() else "scontrol-only"
    d = {"partitions_raw": parse_partitions(f_part.result()), "qos": parse_qos(qos_text),
         "assoc": parse_assoc(assoc_text), "default_account": (f_user.result().strip().splitlines() or [None])[0],
         "slurm_version": f_ver.result().strip(), "mode": mode, "cached_at": int(time.time())}
    try:
        os.makedirs(CACHE, mode=0o700, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(d, fh)
        os.rename(tmp, path)
    except Exception:
        pass
    return d


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="auto")
    ap.add_argument("--tokens", default="none")
    ap.add_argument("--no-cache", action="store_true")
    a = ap.parse_args(argv)
    for v in (a.account, a.tokens):
        if not SAFE.match(v):
            sys.exit("unsafe argument")

    d = discover(a.account, 0 if a.no_cache else 600)
    account, cands, why = choose_account(d["assoc"], d["default_account"], a.account)
    try:
        groups = [g.gr_name for g in grp.getgrall() if os.environ.get("USER") in g.gr_mem] + \
                 [grp.getgrgid(os.getgid()).gr_name]
    except Exception:
        groups = None
    nodes = parse_nodes(sh(["scontrol", "show", "node", "-o"]))
    parts = []
    for p in d["partitions_raw"]:
        if p["hidden"]:
            continue
        usable, reason, qname = partition_access(p, account, groups, d["assoc"])
        q = d["qos"].get(qname) if qname else None
        rows = [r for r in d["assoc"] if r["account"] == account and r["partition"] in (None, p["name"])]
        pn = [n for n in nodes if p["name"] in n["partitions"]]
        parts.append({"name": p["name"], "usable": usable, "why": reason, "qos": qname,
                      "require_qos_flag": bool(qname),
                      "caps": {k: (q or {}).get(k) for k in ("submit_pa", "jobs_pa", "submit_pu", "jobs_pu")},
                      "deny_on_limit": (q or {}).get("deny_on_limit"),
                      "limits": partition_limits(p, q, rows, nodes),
                      "gpu_types": sorted({n["gpu_type"] or "gpu" for n in pn if n["gpu_total"]}),
                      "nodes": [n["node"] for n in pn]})

    jobs = collect_jobs()
    usage = {}
    if account:
        for line in sh(["squeue", "-h", "-A", account, "-t", "PD,R", "-o", "%q|%T|%u"]).splitlines():
            f = (line.split("|") + ["", "", ""])[:3]
            u = usage.setdefault(f[0], {"acct_used": 0, "acct_running": 0, "mine_used": 0, "mine_running": 0})
            u["acct_used"] += 1
            u["acct_running"] += f[1] == "RUNNING"
            if f[2] == os.environ.get("USER"):
                u["mine_used"] += 1
                u["mine_running"] += f[1] == "RUNNING"
    pending = {}
    for line in sh(["squeue", "-h", "-t", "PD", "-o", "%P"]).splitlines():
        for pn in line.split(","):
            pending[pn] = pending.get(pn, 0) + 1

    sessions = match_sessions(collect_sessions(), jobs)
    agents, runs = collect_agents(jobs)
    version = ""
    try:
        version = open(os.path.join(os.path.dirname(os.path.realpath(__file__)), "VERSION")).read().strip()
    except Exception:
        pass
    json.dump({"protocol": PROTOCOL, "gq_version": version, "host": socket.gethostname(), "ts": int(time.time()),
               "discovery": {"account": account, "account_candidates": cands, "account_reason": why,
                             "mode": d["mode"], "slurm_version": d["slurm_version"], "cached_at": d["cached_at"]},
               "partitions": parts, "usage": usage, "pending": pending, "tokens": tokens(a.tokens, account),
               "nodes": nodes, "jobs": jobs, "sessions": sessions, "agents": agents, "runs": runs,
               "errors": ERRORS}, sys.stdout)


if __name__ == "__main__":
    main()
