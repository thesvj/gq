# Troubleshooting

Start with `gq doctor`. It checks every cluster and prints a fix for each failure.

| symptom | cause / fix |
|---|---|
| `missing or unsuitable terminal: xterm-ghostty` (or kitty, foot, …) | Old gq versions only. Current gq picks a terminal type the login node knows. Update: `uv tool upgrade gq`. |
| `ERR ssh …: Permission denied` / hangs | gq uses `BatchMode=yes`: `ssh HOST true` must work with no password prompt. Add a key (`ssh-copy-id HOST`). For 2FA, use `ControlMaster auto` + `ControlPersist 8h` in `~/.ssh/config` and log in once. |
| `account is ambiguous (a, b)` | You belong to several Slurm accounts. Set `account = a` under `[cluster …]`. |
| a partition is missing | gq hides partitions you can't use (AllowAccounts, AllowGroups, DOWN). `gq discover CLUSTER` shows each one with `usable` and `why`. Set `partitions =` to choose which to show. |
| `slots ?/∞` or no caps | `sacctmgr` is not readable for you (scontrol-only mode). Caps can't be checked, so be careful with sites that charge per submit. |
| `FULL` but you think it isn't | The cap counts your **account's** pending+running jobs in that QoS, colleagues included (that is how Slurm's `MaxSubmitPA` works). `squeue -A ACCOUNT -q QOS` shows them. |
| sessions show `~` | The session was not started by gq, so it was matched to its job by srun start time + partition. Sessions started with `gq new` match exactly. |
| `no agent runner in job N` | Agents are off (`agents = true` in config), or the allocation predates gq. In its shell: `source ~/.gq/current/rc --agents`. |
| dashboard shows old data / `⟳` forever | A cluster is slow or unreachable. Its header shows the data's age. Other clusters keep working. |
| `sbatch is blocked inside agent runs` | Intended: agents may not create jobs. Run that command yourself. |
| Slurm is installed somewhere unusual | Create `~/.gq/env` on the cluster: `export PATH=/site/slurm/bin:$PATH` and `export SLURM_CONF=/site/slurm.conf`. |

## FAQ

**Does this break cluster rules?** gq is built to follow the usual rules. On the login node it only
runs read-only Slurm queries, `tmux` and `ps`, once per refresh (≥ 30 s), and only while a dashboard is
open (plus single calls for `gq ls/new/run`). It never computes there. Your shell, your programs and the
agent runner all run on the compute node inside an allocation you requested. Keeping an interactive
allocation open is still subject to your site's policy on idle allocations; the dashboard's `IDLE` marker
helps you notice one. When in doubt, show your admins this page.

**Does an idle `gq` cost anything?** No. Without an open dashboard gq makes no calls at all. Sessions
are ordinary Slurm jobs and end at their wall time.

**What happens if the login node reboots?** tmux dies, the `srun` dies with it, and Slurm ends the
allocation. That is the same as using srun in tmux by hand.

**Can several laptops use the same cluster?** Yes. Releases are immutable and auto-update moves
forward only, so two laptops on different versions never break each other.

**Why tmux on the login node and not `sbatch` + reconnect?** An interactive `srun --pty` is what most
sites allow for interactive work, and tmux keeps it alive across disconnects. gq automates exactly that
habit.
