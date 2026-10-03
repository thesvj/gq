"""gq configuration: ~/.config/gq/config.ini (INI). Everything not set is auto-discovered.

[gq]
refresh = 30            ; dashboard refresh seconds (floor 30: be polite to slurmctld)
policy = half           ; default request size: half | max  (of the per-job limit gq discovers)
auto_update = true      ; redeploy cluster-side files when this gq version is newer
agents = false          ; start the agent runner inside allocations (see docs/ARCHITECTURE.md)
terminal =              ; window command for the GUI launcher, e.g. "kitty {cmd}"; blank = autodetect

[cluster mycluster]
host = mycluster        ; ssh host or ~/.ssh/config alias (key login required)
account = auto          ; slurm account; auto = discover (asks you if several)
tokens = none           ; none | lua_job_submit | cmd:/abs/path/on/login/node
policy = max            ; per-cluster override
partitions = auto       ; or a comma list to show only these
default.short = cpus=8 mem=32G time=2:00:00 gres=gpu:1   ; per-partition request override
cost.long = 1.0         ; override/declare a per-job cost shown in the UI
gpu_name.node01 = A100  ; label for nodes whose gres type is untyped
"""
import configparser
import os
import re
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("GQ_CONFIG_DIR", Path.home() / ".config/gq"))
CONFIG_FILE = CONFIG_DIR / "config.ini"
CACHE_DIR = Path(os.environ.get("GQ_CACHE_DIR", Path.home() / ".cache/gq"))

SAFE = re.compile(r"^[A-Za-z0-9_.:,=@/+%~-]+$")  # values are always shlex-quoted too
HOST = re.compile(r"^[A-Za-z0-9_.@-]+$")
NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


class ConfigError(ValueError):
    pass


def safe(value, what="value"):
    """Every value that reaches a remote command line goes through here."""
    v = str(value).strip()
    if not v or not SAFE.match(v) or v.startswith("-"):
        raise ConfigError(f"unsafe {what}: {value!r}")
    return v


def parse_request(s):
    """'cpus=8 mem=32G time=2:00:00 gres=gpu:1' -> dict, validated."""
    out = {}
    for tok in (s or "").split():
        if "=" not in tok:
            raise ConfigError(f"bad request token {tok!r} (want key=value)")
        k, v = tok.split("=", 1)
        if k not in ("cpus", "mem", "time", "gres", "node"):
            raise ConfigError(f"unknown request key {k!r}")
        out[k] = safe(v, k)
    return out


class Cluster:
    def __init__(self, name, sec, glob):
        if not NAME.match(name):
            raise ConfigError(f"bad cluster name {name!r}")
        self.name = name
        self.host = sec.get("host", name).strip()
        if not HOST.match(self.host) or self.host.startswith("-"):
            raise ConfigError(f"[cluster {name}] unsafe host {self.host!r}")
        self.account = safe(sec.get("account", "auto"), "account")
        self.tokens = safe(sec.get("tokens", "none"), "tokens")
        if not (self.tokens in ("none", "lua_job_submit") or self.tokens.startswith("cmd:/")):
            raise ConfigError(f"[cluster {name}] tokens must be none | lua_job_submit | cmd:/abs/path")
        self.policy = sec.get("policy", glob.get("policy", "half")).strip()
        if self.policy not in ("half", "max"):
            raise ConfigError(f"[cluster {name}] policy must be half or max")
        parts = sec.get("partitions", "auto").strip()
        self.partitions = None if parts == "auto" else [safe(p, "partition") for p in parts.split(",") if p.strip()]
        self.defaults, self.costs, self.gpu_names = {}, {}, {}
        for k, v in sec.items():
            if k.startswith("default."):
                self.defaults[safe(k[8:], "partition")] = parse_request(v)
            elif k.startswith("cost."):
                try:
                    self.costs[safe(k[5:], "partition")] = float(v)
                except ValueError:
                    raise ConfigError(f"[cluster {name}] {k} must be a number")
            elif k.startswith("gpu_name."):
                self.gpu_names[safe(k[9:], "node")] = v.strip()[:16]


class Config:
    def __init__(self, path=CONFIG_FILE):
        self.path = Path(path)
        cp = configparser.ConfigParser(inline_comment_prefixes=(";", "#"), interpolation=None)
        cp.optionxform = str  # keep node/partition names' case
        if self.path.exists():
            cp.read(self.path)
        g = cp["gq"] if cp.has_section("gq") else {}
        try:
            self.refresh = max(30, int(g.get("refresh", 30)))
        except ValueError:
            raise ConfigError("[gq] refresh must be an integer")
        self.auto_update = str(g.get("auto_update", "true")).lower() in ("1", "true", "yes", "on")
        self.agents = str(g.get("agents", "false")).lower() in ("1", "true", "yes", "on")
        self.terminal = (g.get("terminal") or "").strip() or None  # e.g. "kitty {cmd}"; blank = autodetect
        glob = dict(g)
        self.clusters = {}
        for s in cp.sections():
            if s.startswith("cluster "):
                c = Cluster(s.split(None, 1)[1].strip(), cp[s], glob)
                self.clusters[c.name] = c

    def cluster(self, name):
        if name not in self.clusters:
            known = ", ".join(self.clusters) or "none — run `gq setup`"
            raise ConfigError(f"unknown cluster {name!r} (configured: {known})")
        return self.clusters[name]


TEMPLATE = """\
# gq config — see docs/CONFIG.md. Anything not set here is discovered from Slurm.
[gq]
refresh = 30
policy = half
auto_update = true
agents = false

# One section per cluster. `host` is an ssh host/alias with key login.
# [cluster mycluster]
# host = mycluster
# account = auto
# tokens = none
"""
