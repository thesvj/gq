# Configuration

gq reads `~/.config/gq/config.ini`. Every setting is optional except the list of clusters, and
anything you leave out is discovered from Slurm on each refresh. Run `gq discover CLUSTER` to see
exactly what gq learned.

## Full reference

```ini
[gq]
refresh = 30          ; dashboard refresh in seconds. Minimum 30, to be polite to slurmctld.
policy = half         ; default request size: half | max of the discovered per-job limit
auto_update = true    ; redeploy the cluster-side files when your local gq is newer (never downgrades)
agents = false        ; start the agent runner inside new allocations (see ARCHITECTURE.md)
terminal =            ; how the app launcher/shortcut opens a window, e.g. "kitty {cmd}". Blank = autodetect.

[cluster mycluster]   ; the name is yours: it appears in the UI and in `gq run mycluster:...`
host = mycluster      ; ssh host or ~/.ssh/config alias. Key login required (BatchMode).
account = auto        ; Slurm account. auto = discovered; if you have several, gq asks you to set one.
tokens = none         ; cost/quota provider: none | lua_job_submit | cmd:/abs/path/on/login/node
policy = max          ; per-cluster override of [gq] policy
partitions = auto     ; or a comma list to show only these, e.g. "gpu,debug"

; per-partition request overrides (win over policy): cpus mem time gres node
default.gpu = cpus=8 mem=64G time=8:00:00 gres=gpu:a100:1
default.debug = cpus=2 mem=8G time=0:30:00

; declare or override a per-job cost shown in the UI (any unit your site uses)
cost.long = 1.0

; label for nodes whose GPU type Slurm reports untyped ("gpu:8")
gpu_name.node01 = H100
```

Values that reach a remote command line must match `[A-Za-z0-9_.:,=@/+%~-]` and must not start with
`-`. Anything else is rejected when the config loads.

## How discovery works

| what | source (first that works) |
|---|---|
| Slurm binaries / config | `PATH`, then common install dirs (`/opt/slurm/bin`, Bright `/cm/shared/apps/slurm/current/bin`, …); `SLURM_CONF` probed only when `scontrol ping` fails. Add `~/.gq/env` on the cluster (sourced by bash) to force anything. |
| account | config → the only account in your associations → Slurm `DefaultAccount`. Never a guess between several. |
| usable partitions | `scontrol show partition`: State, AllowAccounts/DenyAccounts, AllowGroups, AllowQos, Hidden. Unclear cases show as `unknown` (dimmed) instead of disappearing. |
| QoS per partition | partition `QoS=` → your association's default QoS if allowed → the single allowed QoS |
| caps (`slots x/y`) | QoS `MaxSubmitPA/PU`, `MaxJobsPA/PU`. Counted as your account's pending+running jobs in that QoS. |
| per-job maximum | QoS `MaxTRES` → `MaxTRESPU` → association `MaxTRES` → never more than the smallest node in the partition (and `MaxCPUsPerNode`/`MaxMemPerNode`). A typed GPU (`gres/gpu:3g.71gb=1`) wins over an untyped one. |
| wall time | QoS `MaxWall` → partition `MaxTime` |
| free GPUs | `scontrol show node -o` (`CfgTRES`/`AllocTRES`, nothing truncated); DOWN/DRAIN/FAIL/MAINT nodes count as unavailable; a GPU only counts as *usable* if the node also has the CPUs and memory your request needs |
| your sessions | your tmux panes → their `srun` child → the job with the same `-J` name (`gq-*`), else same partition and srun start within 300 s of the job's submit time (`~` in the UI) |

Slow lookups (partitions, QoS, associations) are cached on the login node for 10 minutes
(`~/.gq/cache`). Node and job state is fresh on every refresh. If `sacctmgr` is unavailable, gq runs in
**scontrol-only** mode: caps show as `∞`/`?`, and the per-job maximum comes from node sizes.

## Adding a cluster

1. Make `ssh NAME` work without a password (an ssh key plus an entry in `~/.ssh/config`). For sites with
   2FA, set up `ControlMaster`/`ControlPersist` in your ssh config, log in once, and gq reuses the connection.
2. `gq setup` and enter the name and host. Or add a `[cluster NAME]` section yourself and run `gq setup`
   to deploy.
3. `gq doctor` checks slurm, tmux and python on the login node and prints a fix for each failure.
4. `gq discover NAME` shows the discovered account, partitions, limits and caps. Override anything that
   is wrong in the config, and please open an issue with the (anonymised) output so discovery can learn it.

## Cost / quota providers (`tokens =`)

Some sites charge each job against a group budget. gq can show the cost before you submit and the
balance in the header. A provider returns:

```json
{"balance": 27.4, "monthly": 36, "cost": {"short": 0.1, "medium": 0.5, "long": 1.0}}
```

- `none` (default): no costs shown, unless you declare `cost.<partition>` in the config.
- `lua_job_submit` (bundled, opt-in): for sites whose `job_submit.lua` (next to `slurm.conf`)
  defines a `TOKEN_COST = { partition = n, ... }` table, `DEFAULT_TOKENS = n` and
  `token_file = "/path/ledger.json"` (a JSON object keyed by account with `tokens_remaining`). The Lua is
  read with regular expressions and **never executed**.
- `cmd:/absolute/path`: your own executable on the login node. gq runs it with the account as its only
  argument, and it must print the JSON above within 10 s. This is how you adapt gq to any site: wrap
  `sshare`, a site API, or a ledger file in a few lines of script.

## Agent runner

`agents = true` writes `~/.gq/agents.enabled` on each cluster. New allocations then start the runner
automatically. For an allocation you started earlier, run `source ~/.gq/current/rc --agents` inside it.
See [ARCHITECTURE.md](ARCHITECTURE.md#agent-runner).
