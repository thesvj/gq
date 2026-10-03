#!/usr/bin/env python3
"""Claude Code PreToolUse(Bash) guard for gq: commands that could spend cluster quota need a human "yes".

This is a guardrail against ACCIDENTS, not a security boundary. It pattern-matches the command text, so
`bash -c "$(...)"`, wrapper scripts on the cluster, aliases or another tool can get past it. The real
controls are gq's human confirmation (TTY or GUI click) and the cluster's own QoS limits.
Agents should run work inside an allocation a human already holds:  gq run CLUSTER:SESSION -- CMD
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SUBMIT = r"\b(srun|sbatch|salloc|scancel|scontrol\s+(update|requeue|release|hold))\b"
OBFUSC = r"(base64|\beval\b|\bxxd\b|\\x[0-9a-fA-F]{2}|\$'\\)"
REMOTE = r"\b(ssh|scp|rsync|mosh|sshpass|autossh)\b"
GQ = r"(^|[\s;&|(`])(\S*/)?gq"
REASON = ("Creating Slurm jobs can spend shared quota (some sites charge per job even when it fails). "
          "Agents should not allocate: use `gq ls` to find a session and `gq run CLUSTER:SESSION -- cmd`, then "
          "`gq wait` / `gq tail` (no new job). Ask the human to start a session with `gq` if none exists.")


def ask(why):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                             "permissionDecisionReason": f"[gq guard] {why}. {REASON}"}}))
    sys.exit(0)


def cluster_hosts():
    """Configured cluster aliases + their real HostNames (cached; `ssh -G` output is never logged)."""
    cache = Path(os.environ.get("GQ_CACHE_DIR", Path.home() / ".cache/gq")) / "guard-hosts.json"
    cfgf = Path(os.environ.get("GQ_CONFIG_DIR", Path.home() / ".config/gq")) / "config.ini"
    try:
        if cache.exists() and cfgf.exists() and cache.stat().st_mtime >= cfgf.stat().st_mtime:
            return json.loads(cache.read_text())
    except Exception:
        pass
    hosts = set()
    try:
        import configparser
        cp = configparser.ConfigParser(inline_comment_prefixes=(";", "#"), interpolation=None)
        cp.read(cfgf)
        for s in cp.sections():
            if s.startswith("cluster "):
                h = cp[s].get("host", s.split(None, 1)[1]).strip()
                hosts.add(h)
                try:
                    out = subprocess.run(["ssh", "-G", h], capture_output=True, text=True, timeout=3).stdout
                    m = re.search(r"^hostname\s+(\S+)", out, re.M)
                    if m:
                        hosts.add(m.group(1))
                except Exception:
                    pass
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(sorted(hosts)))
    except Exception:
        pass
    return sorted(hosts)


def check(cmd, hosts):
    """-> reason string if the command needs a human, else None."""
    if re.search(GQ + r"(\s+(new|cancel|kill|ui|demo|uninstall|install-hook)\b|\s*($|[;&|)]))", cmd):
        return "gq new/cancel/ui are for humans"
    hp = r"(?<![\w.-])(" + "|".join(re.escape(h) for h in hosts) + r")(?![\w.-])" if hosts else None
    to_cluster = bool(re.search(REMOTE, cmd)) and (bool(re.search(hp, cmd)) if hp else True)
    via_run = bool(re.search(GQ + r"\s+run\b", cmd))
    if (to_cluster or via_run) and re.search(SUBMIT, cmd):
        return "command sends srun/sbatch/salloc/scancel to a cluster"
    if to_cluster and re.search(OBFUSC, cmd):
        return "obfuscated command sent to a cluster"
    for path in re.findall(r"(?:^|[\s;&|])(?:bash|sh|zsh|python3?|source|\.)?\s*((?:\.{0,2}/|~/)?[\w./~-]+\.(?:sh|py|bash))\b", cmd):
        p = os.path.expanduser(path)
        try:
            if os.path.isfile(p) and os.path.getsize(p) < 1_000_000:
                body = open(p, errors="ignore").read()
                if re.search(REMOTE, body) and re.search(SUBMIT, body) and (re.search(hp, body) if hp else True):
                    return f"script {path} sends slurm submit commands to a cluster"
        except OSError:
            pass
    return None


def main():
    try:
        data = json.load(sys.stdin)
        cmd = data.get("tool_input", {}).get("command", "") or ""
    except Exception:
        ask("could not parse the tool call (failing closed)")
        return
    why = check(cmd, cluster_hosts())
    if why:
        ask(why)


if __name__ == "__main__":
    main()
