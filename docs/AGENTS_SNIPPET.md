# Paste into CLAUDE.md / AGENTS.md

```markdown
## Slurm clusters: agents never allocate

New Slurm jobs spend shared quota, even when they fail. Do not run srun/sbatch/salloc.
Use an allocation the human already holds:
- `gq ls`: find a session with `agent ● on`
- `gq run CLUSTER:SESSION -C ~/project -- 'python train.py'` prints a run id
- `gq wait CLUSTER ID`: up to 540 s; exit 75 means still running, so call it again (or run it in the background)
- `gq tail CLUSTER ID 40 'error|loss'`: short, cleaned log tail
- `gq stop CLUSTER ID`
If no session has the agent runner on, ask the human to start one (`gq`). Never poll with sleep loops.
```
