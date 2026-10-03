"""gq dashboard (Textual). Paints instantly from cache, refreshes in the background.

Enter / 1-9 returns (cluster, session) to the caller, which runs ssh -t … tmux attach and relaunches the
dashboard after you detach (Ctrl-b d). Exiting + relaunching is more robust across terminals than
suspending a TUI around a nested full-screen program.
"""
import json
import time

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from . import model, remote
from .config import CACHE_DIR

# blue = free/ok, orange = full/warning. Colour is never the only signal: glyphs and words carry it too.
FREE, USED, WARN, DIM, MUTED = "#5fafff", "grey50", "#ff9f43", "grey62", "grey46"


def bar(frac, width=8):
    frac = 0.0 if frac is None else max(0.0, min(1.0, frac))
    n = round(frac * width)
    return "▰" * n + "▱" * (width - n)


def free_line(st):
    """One dense line for a partition: □ free ■ busy × down, count, node detail, slots, cost."""
    t = Text()
    t.append(f"  {st['name']:<10}", style="bold")
    grid = Text()
    for n in st["nodes"]:
        if not n["total"]:
            continue
        if n["bad"]:
            grid.append("×" * n["total"], style=USED)
        else:
            grid.append("□" * n["free"], style=FREE)
            grid.append("■" * (n["total"] - n["free"]), style=USED)
        grid.append(" ")
    t.append_text(grid)
    t.append(" " * max(1, 22 - len(grid.plain)))
    if st["gpu_partition"]:
        k = st["usable_gpus"]
        t.append(f"{k:>2} free ", style=f"bold {FREE}" if k else f"bold {USED}")
        detail = ", ".join(f"{n['label']}·{n['node']} {n['free']}" + (" DOWN" if n["bad"] else "")
                           for n in st["nodes"] if n["total"])
    else:
        t.append(" cpu    ", style=DIM)
        detail = ", ".join(n["node"] for n in st["nodes"])[:40]
    t.append(f" {detail[:44]:<44}", style=DIM)
    cap = st["slots_cap"]
    if st["full"]:
        t.append(f" slots {st['slots_used']}/{cap if cap is not None else '∞'} FULL", style=f"bold {WARN}")
    else:
        t.append(f" slots {st['slots_used']}/{cap if cap is not None else '∞'}", style=FREE)
        if st["will_pend"]:
            t.append(" will pend", style=WARN)
        elif st["gpu_partition"] and not st["usable_gpus"]:
            t.append(" will queue", style=WARN)
    if st["queue"]:
        t.append(f"  queue {st['queue']}", style=DIM)
    if st["cost"] is not None:
        t.append(f"  {st['cost']:g} tok", style=DIM)
    if st["usable"] == "unknown":
        t.append(f"  ? {st['why']}", style=MUTED)
    t.no_wrap, t.overflow = True, "ellipsis"
    return t


class NewScreen(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss(None)", "cancel")]
    CSS = """
    NewScreen { align: center middle; }
    #box { width: 104; height: auto; max-height: 92%; border: round $accent; padding: 1 2; background: $surface; }
    #opts { height: auto; max-height: 12; margin-bottom: 1; }
    .row { height: 3; }
    .row Label { width: 7; padding: 1 0; }
    .row Input { width: 20; }
    #summary { margin: 1 0; }
    #go { width: 100%; }
    """

    def __init__(self, cfg, snaps, preset=None):
        super().__init__()
        self.cfg, self.snaps, self.preset, self.choice = cfg, snaps, preset, None

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label("[b]New session[/b]  ↑↓ pick · Tab to fields · Enter on the button starts it")
            opts = []
            for name, c in self.cfg.clusters.items():
                s = self.snaps.get(name)
                if not s:
                    continue
                for p in model.visible_partitions(c, s):
                    st = model.part_status(c, s, p)
                    lab = Text(f"{name:<12}{p['name']:<10}", style="bold")
                    if st["gpu_partition"]:
                        k = st["usable_gpus"]
                        lab.append(f"{k:>2} free GPU  ", style=f"bold {FREE}" if k else USED)
                    else:
                        lab.append("  cpu only    ", style=DIM)
                    cap = st["slots_cap"]
                    lab.append(f"slots {st['slots_used']}/{cap if cap is not None else '∞'}" +
                               ("  FULL — would be rejected" if st["full"] else ""), style=WARN if st["full"] else FREE)
                    if st["cost"] is not None:
                        lab.append(f"   {st['cost']:g} tok", style=DIM)
                    opts.append(Option(lab, id=f"{name}:{p['name']}", disabled=st["full"] or p["usable"] == "no"))
            yield OptionList(*opts, id="opts")
            with Horizontal(classes="row"):
                yield Label("cpus"); yield Input(id="cpus")
                yield Label("mem"); yield Input(id="mem")
                yield Label("time"); yield Input(id="time")
            with Horizontal(classes="row"):
                yield Label("gres"); yield Input(id="gres", placeholder="none")
                yield Label("node"); yield Input(id="node", placeholder="any")
                yield Label("count"); yield Input("1", id="count", restrict=r"[0-9]*", max_length=2)
            yield Static("", id="summary")
            yield Button("Pick a partition", id="go", variant="warning", disabled=True)

    def on_mount(self):
        ol = self.query_one(OptionList)
        if self.preset:
            try:
                ol.highlighted = ol.get_option_index(self.preset)
            except Exception:
                pass
        ol.focus()

    @on(OptionList.OptionHighlighted)
    def pick(self, ev):
        self.choice = ev.option.id
        name, part = self.choice.split(":")
        c, s = self.cfg.clusters[name], self.snaps[name]
        st = model.part_status(c, s, model.partition(s, part))
        req = st["request"]
        for k in ("cpus", "mem", "time", "gres", "node"):
            self.query_one(f"#{k}", Input).value = req.get(k) or ""
        labels = {n["label"] for n in st["nodes"] if n["total"]}
        best = max((n for n in st["nodes"] if n["fits"] and not n["bad"]), key=lambda n: n["free"], default=None)
        if len(labels) > 1 and best and best["free"] and not req.get("node"):
            self.query_one("#node", Input).value = best["node"]  # several GPU types: preselect the free one
        self.refresh_summary()

    @on(OptionList.OptionSelected)
    def chosen(self, _):
        self.query_one("#cpus", Input).focus()

    @on(Input.Changed)
    def refresh_summary(self, *_):
        if not self.choice:
            return
        name, part = self.choice.split(":")
        c, s = self.cfg.clusters[name], self.snaps[name]
        st = model.part_status(c, s, model.partition(s, part))
        p = model.partition(s, part)
        t = Text()
        t.append(f"{name} / {part}   ", style="bold")
        t.append(f"account {s['discovery']['account']}  qos {p['qos'] or '-'}  ", style=DIM)
        t.append(f"per-job max: {p['limits']['cpus']} cpu, {model.fmt_mem(p['limits']['mem_mb'])}, "
                 f"{p['limits']['gpus'] or 0} gpu, {model.human(p['limits']['time'])}  ({p['limits']['source']})\n", style=DIM)
        if st["cost"] is not None:
            bal = (s.get("tokens") or {}).get("balance")
            t.append(f"cost {st['cost']:g} token" + (f"   balance {bal} → {bal - st['cost']:.2f}" if isinstance(bal, (int, float)) else "") + "\n")
            t.append("charged even if the job fails, pends then gets cancelled, or you cancel it\n", style=WARN)
        if st["will_pend"]:
            t.append("running-jobs cap reached: it will pend\n", style=WARN)
        try:
            n = max(1, int(self.query_one("#count", Input).value or 1))
        except ValueError:
            n = 1
        room = [c - u for c, u in ((st["slots_cap"], st["slots_used"]), (st["mine_cap"], st["mine"])) if c is not None]
        b = self.query_one("#go", Button)
        if room and n > min(room):
            t.append(f"only {max(0, min(room))} slot(s) free here — lower the count\n", style=f"bold {WARN}")
            b.label, b.disabled = "Too many for the free slots", True
        else:
            spend = f" — spend {st['cost'] * n:g} token" if st["cost"] is not None else ""
            b.label = f"Start {n} × {name}/{part}{spend}  (Enter)" if n > 1 else f"Start {name}/{part}{spend}  (Enter)"
            b.disabled = False
        self.query_one("#summary", Static).update(t)

    @on(Button.Pressed, "#go")
    def go(self):
        name, part = self.choice.split(":")
        vals = {k: self.query_one(f"#{k}", Input).value.strip() for k in ("cpus", "mem", "time", "gres", "node")}
        try:
            n = max(1, int(self.query_one("#count", Input).value or 1))
        except ValueError:
            n = 1
        self.dismiss((name, part, vals, n))


class Dashboard(App):
    TITLE = "gq"
    CSS = """
    #top { height: 1; padding: 0 1; background: $panel; }
    #main { height: 1fr; }
    #left { width: 1fr; }
    #sessions { height: auto; max-height: 60%; }
    #side { width: 60; display: none; border-left: tall $panel; padding: 0 1; }
    #side.on { display: block; }
    .h { height: 1; padding: 0 1; color: $text-muted; text-style: bold; margin-top: 1; }
    #free { height: auto; padding: 0 1; }
    #msg { height: 1; padding: 0 1; }
    """
    BINDINGS = [
        Binding("enter", "attach", "attach"), Binding("n", "new", "new"), Binding("k", "kill", "cancel job"),
        Binding("d", "side", "details"), Binding("r", "refresh", "refresh"), Binding("q", "quit", "quit"),
        *[Binding(str(i), f"digit({i})", show=False) for i in range(1, 10)],
        Binding("y", "confirm", show=False),
    ]

    def __init__(self, cfg, demo=False):
        super().__init__()
        self.cfg, self.demo = cfg, demo
        self.result = None
        self.busy, self.rows, self.pending_kill = set(), [], None
        if demo:
            from .demo import demo_config, demo_snaps
            self.cfg, self.snaps = demo_config(), demo_snaps()
        else:
            self.snaps = {}
            for n in cfg.clusters:
                try:
                    s = json.loads((CACHE_DIR / f"{n}.json").read_text())
                    if s.get("protocol") == remote.PROTOCOL:  # ignore caches from other gq versions
                        self.snaps[n] = s
                except Exception:
                    pass

    def compose(self) -> ComposeResult:
        yield Static(id="top")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Label("SESSIONS   1-9 / Enter attach · Ctrl-b d inside tmux brings you back", classes="h")
                yield DataTable(id="sessions", cursor_type="row", zebra_stripes=True)
                yield Label("FREE NOW   □ free GPU  ■ busy  × down   slots = jobs submitted by your account / cap",
                            classes="h")
                yield Static(id="free")
            with VerticalScroll(id="side"):
                yield Static(id="detail")
        yield Static(id="msg")
        yield Footer()

    def on_mount(self):
        t = self.query_one(DataTable)
        for col in ("#", "state", "cluster", "tmux", "job", "part", "GPU", "time left", "agent", "util"):
            t.add_column(col, key=col)
        if not self.cfg.clusters:
            self.say("no clusters configured — quit and run `gq setup`", WARN)
        self.render_all()
        self.set_interval(1, self.render_top)
        if not self.demo:
            self.action_refresh()
            self.set_interval(self.cfg.refresh, self.action_refresh)

    # ------------------------------------------------------------------ data
    def action_refresh(self):
        if self.demo:
            return
        for n in self.cfg.clusters:
            if n not in self.busy:
                self.busy.add(n)
                self.fetch(n)
        self.render_top()

    @work(thread=True)
    def fetch(self, name):
        snap, err = remote.snapshot(self.cfg.clusters[name], self.cfg, self.cfg.auto_update)
        if snap:
            try:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                tmp = CACHE_DIR / f"{name}.tmp"
                tmp.write_text(json.dumps(snap))
                tmp.replace(CACHE_DIR / f"{name}.json")
            except OSError:
                pass
        self.call_from_thread(self.got, name, snap, err)

    def got(self, name, snap, err):
        self.busy.discard(name)
        if snap:
            self.snaps[name] = snap
        if err:
            self.say(f"{name}: {err[:160]}", WARN)
        self.render_all()

    # ------------------------------------------------------------------ render
    def render_top(self):
        t = Text()
        t.append(" gq ", style="bold reverse")
        if self.demo:
            t.append(" DEMO DATA ", style=f"bold {WARN}")
        for n in self.cfg.clusters:
            s = self.snaps.get(n)
            t.append(f"   {n} ", style="bold")
            if not s:
                t.append("waiting" + (" ⟳" if n in self.busy else ""), style=WARN)
                continue
            tok = s.get("tokens") if isinstance(s.get("tokens"), dict) else {}
            if tok.get("balance") is not None:
                mon = tok.get("monthly") or tok["balance"] or 1
                t.append(bar(tok["balance"] / mon, 10), style=FREE if tok["balance"] > mon * 0.15 else WARN)
                t.append(f" {tok['balance']:g}/{mon:g} tok", style=DIM)
            age = int(time.time() - s.get("ts", 0)) if not self.demo else 0
            t.append(f"  {model.human(age) if age >= 60 else str(age) + 's'} ago", style=WARN if age > 2 * self.cfg.refresh else DIM)
            if n in self.busy:
                t.append(" ⟳", style=FREE)
        self.query_one("#top", Static).update(t)

    def render_all(self):
        good = {}
        for n, s in self.snaps.items():
            try:
                model.session_rows([n], {n: s})
                [model.part_status(self.cfg.clusters[n], s, p) for p in model.visible_partitions(self.cfg.clusters[n], s)]
                good[n] = s
            except Exception as e:  # one malformed snapshot must not take the dashboard down
                self.say(f"{n}: unreadable data ({type(e).__name__}); press r to refresh", WARN)
        self.snaps = good
        self.render_top()
        table = self.query_one(DataTable)
        keep = table.cursor_row
        table.clear()
        self.rows = model.session_rows(list(self.cfg.clusters), self.snaps)
        last = ""
        try:
            last = (CACHE_DIR / "last").read_text().strip()
        except OSError:
            pass
        cursor = 0
        for i, r in enumerate(self.rows):
            j, ses, name, s = r["job"], r["session"], r["cluster"], r["snap"]
            stale = not self.demo and time.time() - s.get("ts", 0) > 2 * self.cfg.refresh
            dim = MUTED if (r["kind"] == "shell" or stale) else ""
            if r["kind"] == "job":
                running = j["state"] == "RUNNING"
                st = Text("● run" if running else "◐ pend", style=FREE if running else WARN)
                if ses["dead"]:
                    st = Text("✕ ended", style=WARN)
            elif r["kind"] == "orphan":
                st = Text("! no tmux", style=WARN)
            else:
                st = Text("✕ dead" if ses["dead"] else "○ shell", style=MUTED)
            tm = Text(ses["name"] if ses else "—", style=f"bold {dim}".strip())
            if ses and ses.get("attached"):
                tm.append(" ⧉", style=FREE)
            if ses and ses.get("match") == "guess":
                tm.append(" ~", style=DIM)
            gpu = Text(model.node_gpu(s, j, self.cfg.clusters.get(name)) if j and j.get("node") else
                       (f"({j['reason']})" if j else ""), style=dim)
            left = Text("")
            if j and j["state"] == "RUNNING" and j.get("left") is not None and j.get("limit"):
                left = Text(bar(j["left"] / j["limit"], 6) + f" {model.human(j['left'])}",
                            style=WARN if j["left"] < 1800 else (dim or FREE))
            elif j:
                left = Text(f"limit {model.human(j.get('limit'))}", style=DIM)
            elif ses:
                left = Text(f"fg: {ses['fg']}", style=MUTED)
            agent, util = Text(""), Text("")
            if j and j["state"] == "RUNNING":
                ag = s.get("agents", {}).get(j["id"])
                on_ = bool(ag and ag.get("alive") is not None and ag["alive"] < 60)
                agent = Text("● on" if on_ else "○ off", style=FREE if on_ else MUTED)
                if on_ and ag.get("util") not in (None, "?"):
                    util = Text(f"{ag['util']}%", style=dim)
                    if (ag.get("idle_min") or 0) >= 15:
                        util = Text(f"{ag['util']}% IDLE {ag['idle_min']}m", style=f"bold {WARN}")
            num = str(i + 1) if i < 9 and ses else ""
            table.add_row(Text(num, style="bold"), st, Text(name, style=f"bold {dim}".strip()), tm,
                          Text(j["id"] if j else "", style=dim), Text(j["part"] if j else "", style=dim),
                          gpu, left, agent, util, key=f"r{i}")
            if ses and f"{name} {ses['name']}" == last:
                cursor = i
        if self.rows:
            table.move_cursor(row=min(keep if keep else cursor, len(self.rows) - 1))
        out = Text()
        for n, c in self.cfg.clusters.items():
            s = self.snaps.get(n)
            if not s:
                continue
            d = s.get("discovery", {})
            out.append(f"{n}", style="bold")
            out.append(f"  account {d.get('account') or '? (set in config)'}  ·  {d.get('mode', '')}\n", style=DIM)
            for p in model.visible_partitions(c, s):
                out.append_text(free_line(model.part_status(c, s, p)))
                out.append("\n")
        self.query_one("#free", Static).update(out)
        self.render_side()

    def current(self):
        t = self.query_one(DataTable)
        if not self.rows or t.cursor_row is None or t.cursor_row >= len(self.rows):
            return None
        return self.rows[t.cursor_row]

    def render_side(self):
        if "on" not in self.query_one("#side").classes:
            return
        r = self.current()
        if not r:
            self.query_one("#detail", Static).update("nothing selected")
            return
        t = Text()
        name, ses, j, s = r["cluster"], r["session"], r["job"], r["snap"]
        t.append(f"{name}  {ses['name'] if ses else '(no tmux)'}\n", style="bold")
        if ses:
            t.append(f"tmux: {'pane ended' if ses['dead'] else 'alive'}, fg={ses['fg']}, "
                     f"{'attached elsewhere' if ses['attached'] else 'detached'}\n", style=DIM)
            if ses.get("match") == "guess":
                t.append("~ matched by srun start time + partition (session not started by gq)\n", style=DIM)
            if ses.get("srun"):
                t.append(f"\n{ses['srun']['args']}\n", style="italic")
        if j:
            t.append(f"\njob {j['id']}  {j['state']}  {j['part']}  node {j['node'] or '-'}\n")
            t.append(f"cpus {j['cpus']}  mem {j['mem']}  gres {j['gres']}  qos {j.get('qos', '')}\n", style=DIM)
            started = time.strftime("%a %d %b %H:%M", time.localtime(j["start"])) if j.get("start") else "-"
            t.append(f"started {started}   limit {model.human(j.get('limit'))}   left {model.human(j.get('left'))}\n", style=DIM)
            if j["state"] != "RUNNING":
                t.append(f"reason: {j['reason']}\n", style=WARN)
            ag = s.get("agents", {}).get(j["id"])
            if not ag or ag.get("alive") is None or ag["alive"] > 60:
                t.append("\nagent runner off. To let agents use this job, in its shell:\n  ", style=DIM)
                t.append("source ~/.gq/current/rc --agents\n", style=f"bold {FREE}")
            else:
                t.append(f"\nagent runner on · GPU {ag.get('util')}% · {ag.get('gmem')} MiB · idle {ag.get('idle_min')}m\n", style=FREE)
                t.append(f"gq run {name}:{ses['name'] if ses else j['id']} -- 'cmd'\n", style=DIM)
            runs = [x for x in s.get("runs", []) if x["job"] == j["id"]][:12]
            if runs:
                t.append("\nagent runs\n", style="bold")
                for x in runs:
                    stt = x["status"] + (f" rc={x['rc']}" if x["rc"] is not None else "")
                    sty = FREE if x["status"] == "done" and x["rc"] == "0" else (WARN if x["status"] == "done" else "")
                    t.append(f"  {time.strftime('%d %H:%M', time.localtime(x['t']))}  {x['id']}  ", style=DIM)
                    t.append(stt + "\n", style=sty)
        self.query_one("#detail", Static).update(t)

    @on(DataTable.RowHighlighted)
    def _hl(self, _):
        self.render_side()

    @on(DataTable.RowSelected)
    def _sel(self, _):
        self.action_attach()

    # ------------------------------------------------------------------ actions
    def say(self, msg, style=""):
        self.query_one("#msg", Static).update(Text(msg, style=style))

    def action_side(self):
        self.query_one("#side").toggle_class("on")
        self.render_side()

    def action_digit(self, i):
        if i - 1 < len(self.rows):
            self.query_one(DataTable).move_cursor(row=i - 1)
            self.action_attach()

    def action_attach(self):
        r = self.current()
        if not r or not r["session"]:
            self.say("this job has no tmux session, so it can't be reattached", WARN)
            return
        self.result = (r["cluster"], r["session"]["name"])
        self.exit()

    def action_new(self):
        r = self.current()
        preset = f"{r['cluster']}:{r['job']['part']}" if r and r.get("job") else None
        self.push_screen(NewScreen(self.cfg, self.snaps, preset), self._do_new)

    def _do_new(self, res):
        if not res:
            return
        if self.demo:
            self.say(f"(demo) would start {res[0]}/{res[1]}", WARN)
            return
        self.say(f"starting {res[3]} × {res[0]}/{res[1]} …", FREE)
        self.submit(*res)

    @work(thread=True)
    def submit(self, name, part, vals, count=1):
        from .cli import GQError, do_batch
        try:
            started = do_batch(self.cfg, [(name, part, count, vals)], snaps=self.snaps, interactive=False)
        except (GQError, ValueError) as e:
            self.call_from_thread(self.say, f"not started: {str(e)[:200]}", WARN)
            return
        if len(started) == 1:
            self.result = started[0]
            self.call_from_thread(self.exit)
        else:
            self.call_from_thread(self.say, "started " + ", ".join(s for _, s in started), FREE)
            self.call_from_thread(self.action_refresh)

    def action_kill(self):
        r = self.current()
        if not r or not r["job"]:
            self.say("select a row with a job", WARN)
            return
        self.pending_kill = r
        self.say(f"cancel job {r['job']['id']} on {r['cluster']}? quota/tokens are not refunded.  y = yes, any key = no",
                 f"bold {WARN}")

    def action_confirm(self):
        r, self.pending_kill = self.pending_kill, None
        if r and not self.demo:
            self.say("cancelling…")
            self.cancel(r)

    @work(thread=True)
    def cancel(self, r):
        args = ["cancel", r["job"]["id"]] + ([r["session"]["name"]] if r["session"] else [])
        rc, out = remote.run(self.cfg.clusters[r["cluster"]].host, *args)
        self.call_from_thread(self.say, out.strip()[:200], FREE if rc == 0 else WARN)
        self.call_from_thread(self.action_refresh)

    def on_key(self, ev):
        if self.pending_kill and ev.key != "y":
            self.pending_kill = None
            self.say("cancel aborted")
            ev.prevent_default()  # the aborting key must not also trigger its own binding (e.g. n = new)
            ev.stop()


def run_dashboard(cfg, demo=False):
    app = Dashboard(cfg, demo=demo)
    app.run()
    return app.result
