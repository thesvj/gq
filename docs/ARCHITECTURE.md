# Architecture

```
 laptop                                   cluster login node                       compute node
 ──────                                   ──────────────────                       ────────────
 gq (CLI + Textual dashboard)  ──ssh──▶  ~/.gq/current/gqr     (bash, bookkeeping)
   config.ini, ~/.cache/gq                 snap.py             (read-only queries → JSON)
   one ssh ControlMaster per host          tmux session gq-short-1
                                             └─ srun --pty bash ───────────────────▶ bash (your shell)
 Enter/1-9 → ssh -t host tmux attach                                                  └─ agentd (opt-in)
                                                                                         runs ~/.gq/q/<job>/*.sh
```

- **The dashboard** paints from `~/.cache/gq/<cluster>.json` at once, then refreshes each cluster in a
  background thread (`gqr snap`, one ssh per cluster per refresh). Attaching works by *exiting* the
  dashboard: the `gq` loop runs `ssh -t host tmux attach` and starts the dashboard again after you detach.
  That is more robust across terminals than suspending a TUI around another full-screen program.
- **Starting a session**: `gqr new` re-checks the caps on the cluster, then runs
  `tmux new-session -d` + `respawn-pane "srun -J gq-<part>-<n> … --pty bash --rcfile ~/.gq/current/rc -i"`.
  `remain-on-exit` keeps the pane, so an allocation that ends or is rejected shows Slurm's message
  instead of vanishing.
- **Any terminal**: ssh forwards `$TERM`. Login nodes often lack terminfo for newer terminals
  (ghostty, kitty, wezterm, foot), and tmux then refuses with "missing or unsuitable terminal". The attach
  command keeps your `TERM` if the node knows it and otherwise falls back to `xterm-256color`, then
  `screen-256color`, `xterm`, `vt100`. For the GUI launcher, gq finds a terminal emulator itself (see
  `gq/terminal.py`) or uses `terminal =` from the config.

## Files on the cluster

```
~/.gq/                      mode 700, must be owned by you (checked on every call)
  releases/<ver>+<hash>/    gqr, snap.py, agentd, rc, shim/{sbatch,salloc}, VERSION. Immutable once written.
  current -> releases/…     switched atomically (symlink + rename)
  q/<jobid>/                agent-runner drops: <id>.sh → .run → .log, .rc ; alive, gpu heartbeats
  cache/                    10-minute discovery cache
  agents.enabled            present when `agents = true`
  env                       optional, yours: extra PATH/SLURM_CONF for unusual sites
```

**Deploys** stream a tarball over ssh into `releases/<id>.tmp.XXXX`, rename it into place, then switch
`current`. A dropped connection leaves only a `.tmp` directory, which the next deploy removes.
Allocations pin the release they started with (`rc` resolves `current` once), so an upgrade never
rewrites a script under a running `agentd`. Old releases are pruned, except any still used by a live
runner. Auto-update moves forward only: a newer install on the cluster (from another laptop) is never
downgraded.

## Agent runner

`agentd` runs **inside the allocation, on the compute node**, and is started by `rc` when agents are
enabled. Every 2 s it writes a heartbeat. It runs each `*.sh` file found in `~/.gq/q/<jobid>/`
(only files owned by you) in its own process group, with `GQ_AGENT=1`, and with a `shim/` directory
first on `PATH` that refuses `sbatch` and `salloc`. Every 30 s it records GPU utilisation for the
dashboard's IDLE warning. It exits when the allocation ends.

Why not `srun --overlap`? On some nodes it fails with "Slurmd could not execve job", and every
overlapping step shows up in accounting. The runner needs no Slurm call at all.

Protocol (v1): `gq run` writes `<id>.sh` atomically. The runner renames it to `<id>.run` and writes
`.start`/`.pid`, and the output goes to `.log`. The exit code is written to `.rc` via a temp file and
rename. `.kill` asks the runner to stop the process group (rc 143). `gq wait` polls `status` for at
most 540 s and exits 75 while the run is still going, so agent tool calls never hit their own timeouts.
`gq tail` turns `\r` into newlines, strips ANSI codes, dedupes and caps output at 4 KB, so progress bars
cannot flood an agent's context.

## Security model

- **Human gate for anything that spends quota.** `gq new` and `gq cancel` need a typed confirmation
  on a real TTY, or a click in a GUI dialog. The dashboard needs a keypress on its button. With no TTY
  and no display, gq refuses.
  Caveat: an agent that controls a pseudo-terminal could type the word. That is why the hook below
  exists, and why the cluster's own QoS limits stay the real backstop.
- **Validated inputs.** Every value that reaches a remote command line (config values, discovered names,
  CLI args) must match `[A-Za-z0-9_.:,=@/+%~-]+`, must not start with `-`, and is passed through
  `shlex.quote`. ssh is called with `--` before the host, and hosts beginning with `-` are rejected,
  which rules out `-oProxyCommand` injection.
- **Shared filesystems.** `~/.gq` is forced to mode 700 and must be owned by you. The runner executes
  only files owned by you, with run ids matching `[0-9A-Za-z_-]+`.
- **Read-only on the login node.** `snap.py` only calls `scontrol`, `sacctmgr`, `squeue`, `tmux` and
  `ps`, and reads files. `gqr` adds tmux management and `srun`/`scancel` for sessions you confirmed.
  A test fails if training/GPU commands ever appear in the login-node scripts.
- **The Claude Code hook** (`gq install-hook`) asks you before an agent runs `srun`, `sbatch`, `salloc`
  or `scancel` against a configured cluster, before `gq new`/`cancel`, before obfuscated commands sent
  to a cluster, and before local scripts that do the same. It is a guardrail against accidents, **not
  containment**. It matches command text, so `bash -c "$(…)"`, aliases, scripts that live on the cluster,
  or another agent tool can get past it (see `tests/test_guard.py::KNOWN_BYPASSES`). It fails closed
  (asks) when it cannot parse a tool call.
