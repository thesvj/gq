"""Pure view logic shared by `gq ls`, `gq new` and the dashboard. No I/O here (easy to test)."""
import re


def fmt_mem(mb):
    if mb is None:
        return "?"
    mb = int(mb)
    return f"{mb // 1024}G" if mb >= 1024 and mb % 1024 == 0 else (f"{mb // 1024}G" if mb >= 10240 else f"{mb}M")


def fmt_wall(s):
    """seconds -> slurm D-HH:MM:SS"""
    if not s:
        return "1:00:00"
    d, r = divmod(int(s), 86400)
    h, r = divmod(r, 3600)
    m, sec = divmod(r, 60)
    return f"{d}-{h:02d}:{m:02d}:{sec:02d}" if d else f"{h}:{m:02d}:{sec:02d}"


def human(s):
    if s is None:
        return "?"
    d, s = divmod(int(s), 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    return f"{d}d{h:02d}h" if d else (f"{h}h{m:02d}m" if h else f"{m}m")


def mem_mb(s):
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?)", str(s or "").upper())
    if not m:
        return 0
    return int(float(m.group(1)) * {"": 1, "K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 ** 2}[m.group(2)])


def partition(snap, name):
    return next((p for p in snap.get("partitions", []) if p["name"] == name), None)


def request(cluster, snap, part_name):
    """Effective request for a new session: discovered per-job max, scaled by policy, then config overrides."""
    p = partition(snap, part_name)
    if not p:
        raise ValueError(f"partition {part_name!r} not found on {cluster.name}")
    lim = p["limits"]
    half = cluster.policy == "half"
    cpus = max(1, lim["cpus"] // 2) if half else lim["cpus"]
    mem = max(1024, lim["mem_mb"] // 2) if half else lim["mem_mb"]
    gpus = lim["gpus"]
    if half and gpus > 1:
        gpus = gpus // 2
    gres = None
    if gpus:
        gres = f"gpu:{lim['gpu_type']}:{gpus}" if lim.get("gpu_type") else f"gpu:{gpus}"
    req = {"cpus": str(cpus), "mem": fmt_mem(mem), "time": fmt_wall(lim["time"]), "gres": gres, "node": None}
    req.update(cluster.defaults.get(part_name, {}))
    return req


def cost(cluster, snap, part_name):
    if part_name in cluster.costs:
        return cluster.costs[part_name]
    t = snap.get("tokens") or {}
    return (t.get("cost") or {}).get(part_name)


def gpu_label(node, cluster=None):
    t = node.get("gpu_type")
    if cluster and node["node"] in cluster.gpu_names:
        return cluster.gpu_names[node["node"]]
    if t and re.match(r"^\d+g\.\d+gb$", t):
        return f"MIG {t}"
    return t or "GPU"


def part_status(cluster, snap, p):
    """Everything the UI shows for one partition."""
    req = request(cluster, snap, p["name"])
    need_cpu, need_mem = int(req["cpus"]), mem_mb(req["mem"])
    nodes = [n for n in snap.get("nodes", []) if p["name"] in n["partitions"]]
    free = usable = 0
    per_node = []
    for n in nodes:
        f = 0 if n["bad"] else max(0, n["gpu_total"] - n["gpu_used"])
        fits = n["cpu_idle"] >= need_cpu and n["mem_free_mb"] >= need_mem
        free += f
        usable += f if fits else 0
        per_node.append({"node": n["node"], "label": gpu_label(n, cluster), "free": f, "total": n["gpu_total"],
                         "bad": n["bad"], "fits": fits})
    u = (snap.get("usage") or {}).get(p["qos"] or "", {})
    caps = p.get("caps") or {}
    acct_used, mine = u.get("acct_used", 0), u.get("mine_used", 0)
    full_pa = caps.get("submit_pa") is not None and acct_used >= caps["submit_pa"]
    full_pu = caps.get("submit_pu") is not None and mine >= caps["submit_pu"]
    will_pend = (caps.get("jobs_pa") is not None and u.get("acct_running", 0) >= caps["jobs_pa"]) or \
                (caps.get("jobs_pu") is not None and u.get("mine_running", 0) >= caps["jobs_pu"])
    gpu_part = any(n["gpu_total"] for n in nodes)
    return {"name": p["name"], "usable": p["usable"], "why": p["why"], "nodes": per_node,
            "free": free, "usable_gpus": usable if gpu_part else None, "gpu_partition": gpu_part,
            "slots_used": acct_used, "slots_cap": caps.get("submit_pa"), "mine": mine, "mine_cap": caps.get("submit_pu"),
            "full": full_pa or full_pu, "full_why": "account cap" if full_pa else ("your cap" if full_pu else ""),
            "will_pend": will_pend, "queue": (snap.get("pending") or {}).get(p["name"], 0),
            "cost": cost(cluster, snap, p["name"]), "request": req, "deny_on_limit": p.get("deny_on_limit")}


def visible_partitions(cluster, snap):
    ps = snap.get("partitions", [])
    if cluster.partitions:
        ps = [p for p in ps if p["name"] in cluster.partitions]
    return [p for p in ps if p["usable"] != "no"]


def session_rows(clusters, snaps):
    """Rows for the sessions table: sessions with jobs, jobs without tmux (orphans), plain shells."""
    recs = []
    for name in clusters:
        s = snaps.get(name)
        if not s:
            continue
        jobs = {j["id"]: j for j in s.get("jobs", [])}
        mapped = set()
        for ses in s.get("sessions", []):
            j = jobs.get(ses.get("job")) if ses.get("job") else None
            if j:
                mapped.add(j["id"])
            recs.append({"cluster": name, "session": ses, "job": j, "snap": s,
                         "kind": "job" if j else "shell"})
        for j in s.get("jobs", []):
            if j["id"] not in mapped:
                recs.append({"cluster": name, "session": None, "job": j, "snap": s, "kind": "orphan"})
    order = {"job": 0, "orphan": 1, "shell": 2}
    recs.sort(key=lambda r: (order[r["kind"]], r["cluster"], (r["session"] or {}).get("name", ""), (r["job"] or {}).get("id", "")))
    return recs


def node_gpu(snap, job, cluster=None):
    if not job or not job.get("node"):
        return ""
    n = next((x for x in snap.get("nodes", []) if x["node"] == job["node"]), None)
    if n and n.get("gpu_total"):
        g = job.get("gres") or ""
        m = re.search(r"gpu:(\d+g\.\d+gb)", g)
        return f"{'MIG ' + m.group(1) if m else gpu_label(n, cluster)} {job['node']}"
    return job["node"]
