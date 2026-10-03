"""ssh plumbing + atomic deploy of the cluster-side files (gq/remote_files) to ~/.gq on the login node.

Layout on the cluster:
  ~/.gq/releases/<version>/   immutable once written (gqr, snap.py, agentd, rc, shim/, VERSION)
  ~/.gq/current -> releases/<version>   switched atomically (rename of a symlink)
  ~/.gq/q/<jobid>/            agent-runner file drops       ~/.gq/cache/  discovery cache
Running allocations keep using the release they started with, so upgrades never touch a live agentd.
"""
import hashlib
import io
import json
import shlex
import subprocess
import tarfile
from pathlib import Path

from . import __version__
from .config import CACHE_DIR, ConfigError, safe

FILES = Path(__file__).parent / "remote_files"
PROTOCOL = 1  # must match PROTOCOL in remote_files/snap.py


def ssh_opts():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
            "-o", "ControlMaster=auto", "-o", f"ControlPath={CACHE_DIR}/ssh-%C", "-o", "ControlPersist=10m"]


def release_id():
    """version+content-hash, so any change to the shipped files produces a new immutable release."""
    h = hashlib.sha256()
    for p in sorted(FILES.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            h.update(str(p.relative_to(FILES)).encode())
            h.update(p.read_bytes())
    return f"{__version__}+{h.hexdigest()[:10]}"


def run(host, *args, stdin=None, timeout=60, tty=False):
    """Run `~/.gq/current/gqr <args>` on host. Every arg is validated and shell-quoted."""
    for a in args:
        if a != "-" and not str(a).startswith("--"):
            safe(a, "argument")
    remote = "~/.gq/current/gqr " + " ".join(shlex.quote(str(a)) for a in args)
    cmd = ["ssh", *ssh_opts(), *(["-t"] if tty else []), "--", host, remote]
    try:
        kw = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}  # never eat our stdin
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
    except subprocess.TimeoutExpired:
        return 124, f"ERR ssh {host}: timed out after {timeout}s"
    out = (p.stdout or "") + (p.stderr or "")
    if p.returncode == 255:
        out = f"ERR ssh {host}: {out.strip()[-200:] or 'connection failed'}"
    return p.returncode, out


def _tarball(rid):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for p in sorted(FILES.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                info = t.gettarinfo(str(p), arcname=str(p.relative_to(FILES)))
                info.mode = 0o700 if (p.stat().st_mode & 0o111) else 0o600
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                with open(p, "rb") as fh:
                    t.addfile(info, fh)
        data = (rid + "\n").encode()
        info = tarfile.TarInfo("VERSION")
        info.size, info.mode = len(data), 0o600
        t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def deploy(host, agents=False, timeout=90):
    """Upload a new immutable release and switch ~/.gq/current to it atomically. Idempotent."""
    rid = release_id()
    safe(rid, "release id")
    script = f"""set -e; umask 077
mkdir -p ~/.gq/releases ~/.gq/q ~/.gq/cache; chmod 700 ~/.gq
[ "$(stat -c %u ~/.gq)" = "$(id -u)" ] || {{ echo "ERR ~/.gq not owned by you"; exit 1; }}
cd ~/.gq/releases
if [ ! -d {rid} ]; then
  tmp=$(mktemp -d {rid}.tmp.XXXXXX); tar -xf - -C "$tmp"; mv -T "$tmp" {rid}
else cat >/dev/null; fi
ln -sfn releases/{rid} ~/.gq/current.new && mv -T ~/.gq/current.new ~/.gq/current
{'touch' if agents else 'rm -f'} ~/.gq/agents.enabled
# keep current + 2 newest other releases; never delete one a live agentd may use
ls -1t | grep -v '\\.tmp\\.' | grep -vx {rid} | tail -n +3 | while read -r r; do
  pgrep -u "$(id -u)" -f "releases/$r/agentd" >/dev/null || rm -rf -- "$r"; done
rm -rf -- *.tmp.* 2>/dev/null || true
echo "deployed {rid}"
"""
    cmd = ["ssh", *ssh_opts(), "--", host, "bash -c " + shlex.quote(script)]
    p = subprocess.run(cmd, input=_tarball(rid), capture_output=True, timeout=timeout)
    out = (p.stdout + p.stderr).decode("utf-8", "replace").strip()
    return p.returncode == 0 and "deployed" in out, out


def _semver(v):
    try:
        return tuple(int(x) for x in v.split("+")[0].split("."))
    except Exception:
        return (0,)


def needs_deploy(remote_version):
    """True if the cluster lacks our exact release and is not on a NEWER semver (forward-only)."""
    if not remote_version:
        return True
    if remote_version == release_id():
        return False
    return _semver(remote_version) <= _semver(__version__)


def snapshot(cluster, cfg, auto_update=True):
    """Fetch one snapshot; auto-deploys cluster-side files when missing/outdated. -> (dict|None, err|None)"""
    args = ["snap", "--account", cluster.account, "--tokens", cluster.tokens]
    for attempt in (1, 2):
        rc, out = run(cluster.host, *args, timeout=90)
        snap = None
        if "{" in out:
            try:
                snap = json.loads(out[out.index("{"):])
            except ValueError:
                snap = None
        missing = snap is None and ("No such file" in out or "not found" in out or "no ~/.gq" in out)
        # Protocol mismatch on a NEWER cluster install must not redeploy: that would downgrade it.
        # An older or different build is already covered by needs_deploy.
        stale = snap is not None and needs_deploy(snap.get("gq_version"))
        if (missing or stale) and auto_update and attempt == 1:
            ok, msg = deploy(cluster.host, agents=cfg.agents)
            if not ok:
                return snap, f"deploy to {cluster.name} failed: {msg[-200:]}"
            continue
        if snap is None:
            return None, out.strip()[-300:] or f"no data from {cluster.name}"
        snap["cluster"] = cluster.name
        return snap, None
    return None, "unreachable"


def attach_argv(host, session):
    """ssh argv that attaches to a tmux session from ANY local terminal.
    ssh forwards $TERM; if the login node has no terminfo for it (common for ghostty, kitty, wezterm,
    foot, alacritty) tmux refuses with 'missing or unsuitable terminal'. So the remote side keeps your
    TERM when it is known there and otherwise falls back to the closest widely-installed entry."""
    if not session or not all(c.isalnum() or c in "_.-" for c in session):
        raise ConfigError(f"bad session name {session!r}")
    opts = [o.replace("BatchMode=yes", "BatchMode=no") for o in ssh_opts()]
    remote = ("for t in \"$TERM\" xterm-256color screen-256color xterm vt100; do "
              "infocmp \"$t\" >/dev/null 2>&1 && { export TERM=\"$t\"; break; }; done; "
              f"exec tmux attach -t {shlex.quote('=' + session)}")
    return ["ssh", *opts, "-t", "--", host, remote]
