"""Synthetic data for `gq demo` and README screenshots. Nothing here comes from a real cluster."""
import time

from .config import Config


def demo_config():
    import tempfile
    from pathlib import Path
    p = Path(tempfile.mkdtemp()) / "demo.ini"
    p.write_text("[gq]\nrefresh = 30\npolicy = max\n[cluster alpha]\nhost = alpha\ntokens = lua_job_submit\n"
                 "gpu_name.a-node1 = H200\ngpu_name.a-node2 = B200\n[cluster beta]\nhost = beta\n")
    return Config(p)


def _node(name, part, typ, total, used, cpu=224, idle=150, mem=2_000_000, free=900_000, bad=False):
    return {"node": name, "partitions": [part], "state": "DOWN" if bad else "MIXED", "bad": bad, "gpu_type": typ,
            "gpu_total": total, "gpu_used": used, "cpu_total": cpu, "cpu_idle": idle, "mem_total_mb": mem,
            "mem_free_mb": free}


def _part(name, qos, cpus, mem_gb, gtype, gpus, wall, submit_pa, jobs_pa=None):
    return {"name": name, "usable": "yes", "why": "", "qos": qos, "require_qos_flag": True,
            "caps": {"submit_pa": submit_pa, "jobs_pa": jobs_pa, "submit_pu": None, "jobs_pu": None},
            "deny_on_limit": True, "limits": {"cpus": cpus, "mem_mb": mem_gb * 1024, "gpu_type": gtype, "gpus": gpus,
                                               "time": wall, "source": "qos MaxTRES"}, "gpu_types": [], "nodes": []}


def _job(i, name, part, node, left, limit, gres="gpu:1", state="RUNNING"):
    now = int(time.time())
    return {"id": str(i), "name": name, "part": part, "state": state, "node": node, "submit": now - (limit - left),
            "start": now - (limit - left), "left": left, "limit": limit, "reason": "None", "gres": gres,
            "cpus": "16", "mem": "128G", "qos": part, "account": "lab"}


def _ses(name, job, fg="srun", match="exact", attached=0):
    return {"name": name, "dead": False, "fg": fg, "attached": attached, "created": 0, "job": job, "match": match,
            "srun": {"start": 0, "part": None, "name": name, "args": f"srun -J {name} -A lab ... --pty bash"}}


def demo_snaps():
    now = int(time.time())
    a = {"ts": now, "gq_version": "", "discovery": {"account": "lab", "account_candidates": ["lab"],
         "account_reason": "only association", "mode": "full", "slurm_version": "slurm 24.05"},
         "partitions": [_part("short", "short", 10, 64, "3g.71gb", 1, 6 * 3600, 3),
                        _part("medium", "medium", 16, 256, None, 1, 86400, 2),
                        _part("long", "long", 20, 512, None, 1, 3 * 86400, 1)],
         "usage": {"short": {"acct_used": 1, "acct_running": 1, "mine_used": 1, "mine_running": 1},
                   "medium": {"acct_used": 2, "acct_running": 2, "mine_used": 1, "mine_running": 1},
                   "long": {"acct_used": 0, "acct_running": 0}},
         "pending": {"medium": 4}, "tokens": {"balance": 27.4, "monthly": 36, "cost": {"short": 0.1, "medium": 0.5, "long": 1.0}},
         "nodes": [_node("a-mig", "short", "3g.71gb", 16, 5), _node("a-node1", "medium", None, 8, 6),
                   _node("a-node2", "long", None, 8, 5)],
         "jobs": [_job(4101, "gq-short-1", "short", "a-mig", 4 * 3600 + 900, 6 * 3600, "gpu:3g.71gb:1"),
                  _job(4087, "gq-medium-1", "medium", "a-node1", 15 * 3600, 86400),
                  _job(4120, "bash", "medium", None, 86400, 86400, state="PENDING")],
         "sessions": [_ses("gq-short-1", "4101", attached=1), _ses("gq-medium-1", "4087"),
                      _ses("notes", None, fg="bash", match=None)],
         "agents": {"4101": {"alive": 3, "util": "87", "gmem": "41000", "idle_min": 0},
                    "4087": {"alive": 4, "util": "0", "gmem": "1200", "idle_min": 42}},
         "runs": [{"job": "4101", "id": "1003-1012-77", "status": "done", "rc": "0", "t": now - 600},
                  {"job": "4101", "id": "1003-1040-12", "status": "running", "rc": None, "t": now - 120}]}
    b = {"ts": now, "gq_version": "", "discovery": {"account": "lab", "account_candidates": ["lab"],
         "account_reason": "slurm DefaultAccount", "mode": "full", "slurm_version": "slurm 23.11"},
         "partitions": [_part("gpu", "normal", 32, 240, None, 1, 2 * 86400, None, 2),
                        _part("cpu", "normal", 64, 250, None, 0, 7 * 86400, None)],
         "usage": {"normal": {"acct_used": 1, "acct_running": 1}}, "pending": {}, "tokens": None,
         "nodes": [_node("b-gpu1", "gpu", "a100", 4, 1), _node("b-gpu2", "gpu", "h100", 4, 4),
                   _node("b-gpu3", "gpu", "a100", 4, 0, bad=True),
                   _node("b-cpu1", "cpu", None, 0, 0, cpu=128, idle=90)],
         "jobs": [_job(99812, "bash", "gpu", "b-gpu1", 30 * 3600, 2 * 86400)],
         "sessions": [_ses("train", "99812", match="guess")], "agents": {}, "runs": []}
    for s in (a, b):
        for p in s["partitions"]:
            p["nodes"] = [n["node"] for n in s["nodes"] if p["name"] in n["partitions"]]
    return {"alpha": a, "beta": b}
