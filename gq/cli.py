"""gq command line. Run `gq help` for the summary."""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__, model, remote, terminal
from .config import CACHE_DIR, CONFIG_FILE, TEMPLATE, Config, ConfigError, safe

HELP = """gq — Slurm sessions held in tmux on the login node, one keypress away.

  gq | gq ui | gq menu    dashboard: your sessions, free GPUs, 1-9/Enter attach, n new, k cancel
  gq ls                   the same as plain text
  gq new CL PART [-n COUNT] [--cpus N --mem 32G --time 4:00:00 --gres gpu:1 --node N]   (asks you to confirm)
  gq new CL:PART[:COUNT] [CL:PART[:COUNT] ...]     several sessions/clusters at once, one confirmation
  gq attach CL SESSION    gq cancel CL JOBID|SESSION
  gq setup                add a cluster, deploy, discover        gq doctor   check everything
  gq discover CL          show what gq learned from Slurm        gq demo     dashboard with fake data

 for agents (no new jobs, nothing typed into your shell):
  gq run CL:SESSION|JOBID [-C DIR] [-f FILE] -- CMD...   -> run id
  gq wait CL ID [SECS<=540]   gq status CL ID   gq tail CL ID [N] [REGEX]   gq stop CL ID

  gq install-hook | install-shortcut | uninstall [--yes] | version
Docs: https://github.com/thesvj/gq"""


class GQError(Exception):
    def __init__(self, msg, code=1):
        super().__init__(msg)
        self.code = code


def die(msg, code=1):
    print(f"gq: {msg}", file=sys.stderr)
    sys.exit(code)


def load():
    try:
        return Config()
    except ConfigError as e:
        die(f"config {CONFIG_FILE}: {e}")


def cache_path(name):
    return CACHE_DIR / f"{name}.json"


def fetch_all(cfg, names=None):
    from concurrent.futures import ThreadPoolExecutor
    names = names or list(cfg.clusters)
    out, errs = {}, {}
    with ThreadPoolExecutor(max(1, len(names))) as ex:
        futs = {n: ex.submit(remote.snapshot, cfg.clusters[n], cfg, cfg.auto_update) for n in names}
    for n, f in futs.items():
        snap, err = f.result()
        if snap:
            out[n] = snap
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = cache_path(n).with_suffix(".tmp")
            tmp.write_text(json.dumps(snap))
            tmp.replace(cache_path(n))
        if err:
            errs[n] = err
    return out, errs


# ----------------------------------------------------------------------------- human gate
def confirm(title, text, word):
    """Spending quota is a human decision: typed word on a real TTY, or a GUI click."""
    if sys.stdin.isatty() and sys.stdout.isatty():
        print(text)
        try:
            ans = input(f"type '{word}' to confirm: ").strip()
        except EOFError:
            return False
        return ans == word
    if (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and shutil.which("zenity"):
        return subprocess.call(["zenity", "--question", "--no-markup", "--width=520",
                                f"--title={title}", f"--text={text}"], stderr=subprocess.DEVNULL) == 0
    die("this action needs a human: run it in a terminal (or with a desktop session for the GUI prompt)")


def describe_new(cluster, snap, st, req):
    c = st["cost"]
    lines = [f"Start a session on {cluster.name} / {st['name']}",
             "  srun -A {a} -p {p}{q} --cpus-per-task={cpus} --mem={mem} --time={time}{g}{n}".format(
                 a=snap["discovery"]["account"], p=st["name"],
                 q=f" -q {model.partition(snap, st['name'])['qos']}" if model.partition(snap, st['name'])["qos"] else "",
                 g=f" --gres={req['gres']}" if req.get("gres") else "", n=f" --nodelist={req['node']}" if req.get("node") else "",
                 **req)]
    if c is not None:
        bal = (snap.get("tokens") or {}).get("balance")
        lines.append(f"  cost: {c} per job" + (f"  (balance {bal})" if bal is not None else "") +
                     " — charged even if the job fails or you cancel it")
    if st["usable_gpus"] == 0 and st["gpu_partition"]:
        lines.append("  no GPU free right now: the job will wait in the queue")
    if st["will_pend"]:
        lines.append("  running-jobs cap reached: the job will pend until one of yours/your group's ends")
    if st["deny_on_limit"] is False:
        lines.append("  note: this QoS does not reject over-limit jobs, they queue instead")
    return "\n".join(lines)


def _snap_for(cfg, name, snaps):
    if snaps and name in snaps:
        return snaps[name]
    got, errs = fetch_all(cfg, [name])
    if name not in got:
        raise GQError(errs.get(name, "no data"))
    if snaps is not None:
        snaps[name] = got[name]
    return got[name]


def plan_batch(cfg, items, snaps=None):
    """items: [(cluster, partition, count, overrides)] -> validated plan. Nothing is submitted here.
    Refuses the whole batch if any part would exceed a submit cap (a rejected submit can still be charged)."""
    snaps = {} if snaps is None else snaps
    merged = {}
    for name, part, n, ov in items:
        if n < 1:
            raise GQError(f"count must be >= 1 ({name}/{part})")
        key = (name, part)
        if key in merged:
            merged[key]["count"] += n
        else:
            merged[key] = {"count": n, "overrides": ov}
    plan = []
    for (name, part), it in merged.items():
        cluster = cfg.cluster(name)
        snap = _snap_for(cfg, name, snaps)
        d = snap["discovery"]
        if not d.get("account"):
            raise GQError(f"{name}: account is ambiguous ({', '.join(d['account_candidates'])}). "
                          f"Set `account = ...` under [cluster {name}] in {CONFIG_FILE}")
        p = model.partition(snap, part)
        if not p:
            raise GQError(f"{name}: no partition {part!r}. Known: {', '.join(x['name'] for x in snap['partitions'])}")
        if p["usable"] == "no":
            raise GQError(f"{name}/{part} not usable: {p['why']}")
        st = model.part_status(cluster, snap, p)
        req = dict(st["request"])
        for k, v in (it["overrides"] or {}).items():
            if v:
                req[k] = safe(v, k)
        room = [c - u for c, u in ((st["slots_cap"], st["slots_used"]), (st["mine_cap"], st["mine"])) if c is not None]
        free_slots = min(room) if room else None
        if free_slots is not None and it["count"] > free_slots:
            raise GQError(f"{name}/{part}: asked for {it['count']}, only {max(0, free_slots)} slot(s) free "
                          f"({st['slots_used']}/{st['slots_cap']} used by your account). Nothing submitted.", 3)
        plan.append({"cluster": cluster, "snap": snap, "part": p, "status": st, "request": req, "count": it["count"]})
    return plan


def describe_batch(plan):
    lines, total = [], {}
    for x in plan:
        txt = describe_new(x["cluster"], x["snap"], x["status"], x["request"])
        head, rest = txt.split("\n", 1) if "\n" in txt else (txt, "")
        lines.append(f"{head}   × {x['count']}" + ("\n" + rest if rest else ""))
        if x["status"]["cost"] is not None:
            total[x["cluster"].name] = total.get(x["cluster"].name, 0) + x["status"]["cost"] * x["count"]
    n = sum(x["count"] for x in plan)
    if total:
        lines.append("TOTAL: " + ", ".join(f"{c} {v:g} token" for c, v in total.items()) + f"  for {n} session(s)")
    return "\n".join(lines), n


def execute_batch(plan):
    """Submit the plan. Stops at the first failure; returns [(cluster, session)] started so far."""
    started = []
    for x in plan:
        c, snap, p, req = x["cluster"], x["snap"], x["part"], x["request"]
        part = p["name"]
        taken = {s["name"] for s in snap.get("sessions", [])} | {j["name"] for j in snap.get("jobs", [])} | \
                {s for cl, s in started if cl == c.name}
        caps = p.get("caps") or {}
        for _ in range(x["count"]):
            i = 1
            while f"gq-{part}-{i}" in taken:
                i += 1
            sess = f"gq-{part}-{i}"
            rc, out = remote.run(c.host, "new", sess, part, p["qos"] or "-", snap["discovery"]["account"],
                                 req["cpus"], req["mem"], req["time"], req.get("gres") or "-", req.get("node") or "-",
                                 str(caps["submit_pa"]) if caps.get("submit_pa") is not None else "-",
                                 str(caps["submit_pu"]) if caps.get("submit_pu") is not None else "-")
            last = out.strip().splitlines()[-1] if out.strip() else ""
            if rc != 0 or last != sess:
                done = ", ".join(f"{a}:{b}" for a, b in started) or "none"
                raise GQError(f"stopped at {c.name}/{part}: {out.strip()[-250:]}  (already started: {done})")
            taken.add(sess)
            started.append((c.name, sess))
    return started


def do_batch(cfg, items, snaps=None, interactive=True):
    plan = plan_batch(cfg, items, snaps)
    text, n = describe_batch(plan)
    word = "yes" if n > 1 else plan[0]["part"]["name"]
    if interactive and not confirm("gq: start sessions?", text, word):
        raise GQError("cancelled", 1)
    return execute_batch(plan)


def do_new(cfg, name, part, overrides, snaps=None, interactive=True, count=1):
    """One partition, `count` sessions. Returns the first session name."""
    return do_batch(cfg, [(name, part, count, overrides)], snaps, interactive)[0][1]


def parse_specs(args):
    """['alpha:short:2', 'beta:long'] -> [(cluster, part, n, {})]"""
    out = []
    for a in args:
        f = a.split(":")
        if len(f) not in (2, 3) or not all(f) or (len(f) == 3 and not f[2].isdigit()):
            raise GQError(f"bad spec {a!r}: want CLUSTER:PARTITION[:COUNT]")
        out.append((f[0], f[1], int(f[2]) if len(f) == 3 else 1, {}))
    return out


def attach(cfg, name, session):
    cluster = cfg.cluster(name)
    (CACHE_DIR / "last").write_text(f"{name} {session}")
    env = dict(os.environ)
    env.pop("TMUX", None)
    if sys.stdin.isatty():
        sys.stdout.write(f"\033]0;{name} · {session}\007")  # window title says where you are
        sys.stdout.flush()
        return subprocess.call(remote.attach_argv(cluster.host, session), env=env)
    terminal.open_window(remote.attach_argv(cluster.host, session), cfg.terminal)
    return 0


# ----------------------------------------------------------------------------- commands
def relaunch_argv(extra):
    """Argv for a new terminal. Use the `gq` console script, never `python -m gq`.

    Desktop launches start in $HOME. A checkout at ~/gq is a namespace package on sys.path
    and shadows the installed one, so `python -m gq` dies with ImportError before the UI opens.
    """
    arg0 = Path(sys.argv[0])
    if arg0.is_file() and os.access(arg0, os.X_OK) and arg0.name.startswith("gq"):
        return [str(arg0), *extra]
    found = shutil.which("gq")
    if found:
        return [found, *extra]
    return [sys.executable, "-m", "gq", *extra]


def cmd_ui(cfg, demo=False):
    if not sys.stdin.isatty():  # launched from a shortcut/app icon: open a terminal running ourselves
        terminal.open_window(relaunch_argv(["demo" if demo else "ui"]), cfg.terminal)
        return
    from .tui import run_dashboard
    while True:
        target = run_dashboard(cfg, demo=demo)
        if not target:
            return
        if demo:
            print(f"(demo) would attach to {target}")
            return
        name, session = target
        if attach(cfg, name, session) != 0:
            input("attach failed — press Enter to go back")


def cmd_ls(cfg):
    snaps, errs = fetch_all(cfg)
    for name, c in cfg.clusters.items():
        s = snaps.get(name)
        if not s:
            print(f"== {name}: {errs.get(name, 'no data')}")
            continue
        d, t = s["discovery"], s.get("tokens") or {}
        tok = f"  tokens {t.get('balance')}/{t.get('monthly')}" if t and t.get("balance") is not None else ""
        print(f"== {name}  account {d['account'] or '?'}  ({d['mode']}, {d['slurm_version']}){tok}")
        for r in model.session_rows([name], {name: s}):
            j, ses = r["job"], r["session"]
            state = (j["state"] if j else ("dead" if ses["dead"] else "shell"))
            left = model.human(j["left"]) if j and j.get("left") is not None else ""
            mark = " ~" if ses and ses.get("match") == "guess" else ""
            print(f"  {state:<9} tmux {((ses or {}).get('name', '-') + mark):<16} job {(j or {}).get('id', '-'):<8} "
                  f"{(j or {}).get('part', ''):<8} {model.node_gpu(s, j, c):<20} {left}")
        for p in model.visible_partitions(c, s):
            st = model.part_status(c, s, p)
            free = f"{st['usable_gpus']} GPU free" if st["gpu_partition"] else "cpu"
            cap = f"slots {st['slots_used']}/{st['slots_cap'] if st['slots_cap'] is not None else '∞'}"
            flag = " FULL" if st["full"] else (" will-pend" if st["will_pend"] else "")
            cost = f"  cost {st['cost']}" if st["cost"] is not None else ""
            print(f"  free  {p['name']:<10} {free:<14} {cap}{flag}{cost}")
        for e in s.get("errors", [])[:3]:
            print(f"  ! {e}")


def cmd_run(cfg, a):
    if ":" not in a.target:
        die("target is CLUSTER:SESSION or CLUSTER:JOBID")
    name, t = a.target.split(":", 1)
    c = cfg.cluster(name)
    rc, j = remote.run(c.host, "jobid", t)
    j = j.strip()
    if rc or not j.isdigit():
        die(f"no running job for {a.target} {j}")
    if a.file:
        body = Path(a.file).read_text()
    elif len(a.command) == 1:
        body = a.command[0] + "\n"
    elif a.command:
        import shlex
        body = " ".join(shlex.quote(x) for x in a.command) + "\n"
    else:
        body = sys.stdin.read()
    rc, out = remote.run(c.host, "run", j, a.C or "~", stdin=body)
    print(out.strip())
    sys.exit(rc)


def cmd_setup(cfg):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(TEMPLATE)
        print(f"wrote {CONFIG_FILE}")
    if sys.stdin.isatty():
        name = input("add a cluster? name (empty = skip): ").strip()
        if name:
            host = input(f"ssh host/alias for {name} [{name}]: ").strip() or name
            safe(name, "name"), safe(host, "host")
            with open(CONFIG_FILE, "a") as fh:
                fh.write(f"\n[cluster {name}]\nhost = {host}\naccount = auto\ntokens = none\n")
            cfg = load()
    for name, c in cfg.clusters.items():
        ok, msg = remote.deploy(c.host, agents=cfg.agents)
        print(f"{name}: {msg.splitlines()[-1] if msg else ''}")
        if not ok:
            continue
        snap, err = remote.snapshot(c, cfg, auto_update=False)
        if not snap:
            print(f"  ! {err}")
            continue
        d = snap["discovery"]
        print(f"  account {d['account'] or '?'} ({d['account_reason']}); partitions: " +
              ", ".join(f"{p['name']}{'' if p['usable'] == 'yes' else '(' + p['usable'] + ')'}" for p in snap["partitions"]))
        if not d["account"] and d["account_candidates"]:
            print(f"  ! several accounts: {', '.join(d['account_candidates'])} — set `account =` in [cluster {name}]")


def cmd_doctor(cfg):
    ok = True
    print(f"gq {__version__} (release {remote.release_id()})  python {sys.version.split()[0]}")
    try:
        import textual
        print(f"textual {textual.__version__}")
    except ImportError:
        ok = False
        print("textual MISSING — reinstall gq (pipx/uv/pip)")
    print(f"config {CONFIG_FILE} {'ok' if CONFIG_FILE.exists() else 'MISSING — run gq setup'}")
    for name, c in cfg.clusters.items():
        rc, out = remote.run(c.host, "doctor", timeout=40)
        if rc:
            ok = False
            print(f"== {name}: {out.strip()[-200:]}\n   fix: check `ssh {c.host}` works without a password, then `gq setup`")
        else:
            print(f"== {name}\n" + "\n".join("   " + x for x in out.strip().splitlines()))
    sys.exit(0 if ok else 1)


def cmd_install_hook():
    hook = Path(__file__).parent / "hooks" / "claude_guard.py"
    st = Path.home() / ".claude" / "settings.json"
    st.parent.mkdir(parents=True, exist_ok=True)
    d = json.loads(st.read_text()) if st.exists() else {}
    if st.exists():
        shutil.copy(st, st.with_suffix(".json.bak-gq"))
    pre = d.setdefault("hooks", {}).setdefault("PreToolUse", [])
    pre[:] = [h for h in pre if "claude_guard" not in json.dumps(h) and "gq-guard" not in json.dumps(h)]
    pre.append({"matcher": "Bash", "hooks": [{"type": "command", "command": f"{sys.executable} {hook}", "timeout": 10}]})
    st.write_text(json.dumps(d, indent=2))
    print(f"installed Claude Code guard hook in {st} (backup .bak-gq). Best-effort; see docs/ARCHITECTURE.md")


def cmd_install_shortcut():
    exe = shutil.which("gq") or f"{sys.executable} -m gq"
    apps = Path.home() / ".local/share/applications"
    apps.mkdir(parents=True, exist_ok=True)
    (apps / "gq.desktop").write_text(f"[Desktop Entry]\nType=Application\nName=GPU sessions (gq)\n"
                                     f"Comment=Slurm sessions on your clusters\nExec={exe} ui\nIcon=utilities-terminal\n"
                                     "Terminal=false\nCategories=Development;\n")
    print(f"app launcher: {apps / 'gq.desktop'}")
    if shutil.which("gsettings") and "GNOME" in os.environ.get("XDG_CURRENT_DESKTOP", ""):
        base = "org.gnome.settings-daemon.plugins.media-keys"
        path = f"/{base.replace('.', '/')}/custom-keybindings/gq/"
        cur = subprocess.run(["gsettings", "get", base, "custom-keybindings"], capture_output=True, text=True).stdout.strip()
        if path not in cur:
            lst = [] if cur in ("@as []", "[]", "") else [x.strip(" '") for x in cur.strip("[]").split(",")]
            subprocess.run(["gsettings", "set", base, "custom-keybindings", str(lst + [path])])
        schema = f"{base}.custom-keybinding:{path}"
        for k, v in (("name", "gq"), ("command", f"{exe} ui"), ("binding", "<Super>g")):
            subprocess.run(["gsettings", "set", schema, k, v])
        print("GNOME shortcut: Super+G")
    else:
        print(f"bind a key in your desktop to run: {exe} ui")


def cmd_uninstall(cfg, yes):
    plan = [f"local: {CONFIG_FILE.parent}", f"local: {CACHE_DIR}",
            f"local: {Path.home() / '.local/share/applications/gq.desktop'}",
            "local: Claude hook entry in ~/.claude/settings.json (if installed)"]
    plan += [f"remote {n}: ~/.gq (only if no running allocation still uses it)" for n in cfg.clusters]
    print("gq uninstall will remove:\n  " + "\n  ".join(plan))
    if not yes:
        print("dry run. Re-run with --yes. Then remove the package: pipx uninstall gq  (or uv tool uninstall gq)")
        return
    for n, c in cfg.clusters.items():
        subprocess.run(["ssh", *remote.ssh_opts(), "--", c.host,
                        "pgrep -u $(id -u) -f '.gq/releases/.*/agentd' >/dev/null && echo 'kept ~/.gq (agent runner live)' "
                        "|| { rm -rf ~/.gq && echo removed ~/.gq; }"])
    st = Path.home() / ".claude/settings.json"
    if st.exists():
        d = json.loads(st.read_text())
        pre = d.get("hooks", {}).get("PreToolUse", [])
        pre[:] = [h for h in pre if "claude_guard" not in json.dumps(h)]
        st.write_text(json.dumps(d, indent=2))
    for p in (CONFIG_FILE.parent, CACHE_DIR):
        shutil.rmtree(p, ignore_errors=True)
    (Path.home() / ".local/share/applications/gq.desktop").unlink(missing_ok=True)
    print("done. GNOME shortcut (if any): Settings > Keyboard > Custom Shortcuts > remove 'gq'")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        argv = ["ui"]
    if argv[0] in ("-h", "--help", "help"):
        print(HELP)
        return
    if argv[0] in ("version", "--version"):
        print(f"gq {__version__} (release {remote.release_id()})")
        return
    run_cmd = []
    if argv[0] == "run" and "--" in argv:  # everything after -- is the command, verbatim
        i = argv.index("--")
        argv, run_cmd = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(prog="gq", add_help=False)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("ui"), sub.add_parser("menu"), sub.add_parser("demo"), sub.add_parser("ls"), sub.add_parser("setup")
    sub.add_parser("doctor"), sub.add_parser("install-hook"), sub.add_parser("install-shortcut")
    p = sub.add_parser("uninstall"); p.add_argument("--yes", action="store_true")
    p = sub.add_parser("discover"); p.add_argument("cluster")
    p = sub.add_parser("deploy"); p.add_argument("cluster", nargs="?")
    p = sub.add_parser("new"); p.add_argument("targets", nargs="+")
    p.add_argument("-n", "--count", type=int, default=1)
    for f in ("cpus", "mem", "time", "gres", "node"):
        p.add_argument(f"--{f}")
    p = sub.add_parser("attach"); p.add_argument("cluster"); p.add_argument("session")
    p = sub.add_parser("cancel"); p.add_argument("cluster"); p.add_argument("what")
    p = sub.add_parser("kill"); p.add_argument("cluster"); p.add_argument("what")
    p = sub.add_parser("run"); p.add_argument("target"); p.add_argument("-C"); p.add_argument("-f", dest="file")
    for c in ("wait", "status", "tail", "stop"):
        p = sub.add_parser(c); p.add_argument("cluster"); p.add_argument("id"); p.add_argument("rest", nargs="*")
    a = ap.parse_args(argv)
    cfg = load()
    try:
        if a.cmd in ("ui", "menu"):
            cmd_ui(cfg)
        elif a.cmd == "demo":
            cmd_ui(cfg, demo=True)
        elif a.cmd == "ls":
            cmd_ls(cfg)
        elif a.cmd == "new":
            ov = {k: getattr(a, k) for k in ("cpus", "mem", "time", "gres", "node")}
            if any(":" in t for t in a.targets):
                items = parse_specs(a.targets)
                if any(ov.values()) or a.count != 1:
                    die("--cpus/--mem/... and -n apply to the `gq new CLUSTER PARTITION` form only")
            elif len(a.targets) == 2:
                items = [(a.targets[0], a.targets[1], a.count, ov)]
            else:
                die("usage: gq new CLUSTER PARTITION [-n N] [--cpus ...]  or  gq new CL:PART[:N] [CL:PART[:N] ...]")
            started = do_batch(cfg, items)
            for c, s_ in started:
                print(f"started {c}:{s_}")
            if len(started) == 1:
                print("(Ctrl-b d detaches; run `gq` to come back)")
                attach(cfg, *started[0])
            else:
                print("open the dashboard with `gq` — each session is a numbered row (1-9 attaches)")
        elif a.cmd == "attach":
            sys.exit(attach(cfg, a.cluster, a.session))
        elif a.cmd in ("cancel", "kill"):
            c = cfg.cluster(a.cluster)
            what = safe(a.what, "job/session")
            jid = what if what.isdigit() else remote.run(c.host, "jobid", what)[1].strip()
            if not jid.isdigit():
                die(f"no job for {what}")
            if not confirm("gq: cancel job?", f"Cancel job {jid} on {a.cluster} ({what})? Quota/tokens are not refunded.", "yes"):
                die("cancelled", 1)
            rc, out = remote.run(c.host, "cancel", jid, *([] if what.isdigit() else [what]))
            print(out.strip())
            sys.exit(rc)
        elif a.cmd == "run":
            a.command = run_cmd
            cmd_run(cfg, a)
        elif a.cmd in ("wait", "status", "tail", "stop"):
            rc, out = remote.run(cfg.cluster(a.cluster).host, a.cmd, a.id, *a.rest, timeout=600)
            print(out.rstrip())
            sys.exit(rc)
        elif a.cmd == "setup":
            cmd_setup(cfg)
        elif a.cmd == "doctor":
            cmd_doctor(cfg)
        elif a.cmd == "discover":
            c = cfg.cluster(a.cluster)
            rc, out = remote.run(c.host, "snap", "--account", c.account, "--tokens", c.tokens, "--no-cache", timeout=120)
            s = json.loads(out[out.index("{"):]) if "{" in out else {"error": out}
            print(json.dumps({k: s.get(k) for k in ("discovery", "partitions", "tokens", "errors")}, indent=2))
        elif a.cmd == "deploy":
            for n in ([a.cluster] if a.cluster else cfg.clusters):
                ok, msg = remote.deploy(cfg.cluster(n).host, agents=cfg.agents)
                print(f"{n}: {msg}")
        elif a.cmd == "install-hook":
            cmd_install_hook()
        elif a.cmd == "install-shortcut":
            cmd_install_shortcut()
        elif a.cmd == "uninstall":
            cmd_uninstall(cfg, a.yes)
        else:
            print(HELP)
    except ConfigError as e:
        die(str(e))
    except GQError as e:
        die(str(e), e.code)
    except KeyboardInterrupt:
        sys.exit(130)
