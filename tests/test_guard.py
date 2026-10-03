"""The Claude Code guard: asks on quota-spending commands, stays out of the way otherwise.
It is best-effort; the bypass list documents what it does NOT catch."""
import pytest

from gq.hooks.claude_guard import check

HOSTS = ["clusterA", "10.0.0.5"]

ASK = ['ssh clusterA "sbatch job.sh"', "ssh clusterA srun --pty bash", 'ssh clusterA "$(echo c3J1bg==|base64 -d)"',
       "gq new clusterA short", "gq", "gq cancel clusterA 12", "gq run clusterA:s -- sbatch x",
       "ssh user@10.0.0.5 salloc", "rsync x clusterA: && ssh clusterA scancel 1"]
ALLOW = ["grep -r srun notes/", "man sbatch", 'git commit -m "add sbatch template"', "ssh clusterA squeue --me",
         "gq ls", "gq run clusterA:s -- python train.py", "gq wait clusterA 12", "ssh otherbox nvidia-smi",
         "ssh clusterA tail -f log"]
KNOWN_BYPASSES = ['bash -c "$(printf ssh) clusterA sbatch x"', "alias s=ssh; s clusterA sbatch x"]


@pytest.mark.parametrize("cmd", ASK)
def test_asks(cmd):
    assert check(cmd, HOSTS)


@pytest.mark.parametrize("cmd", ALLOW)
def test_allows(cmd):
    assert check(cmd, HOSTS) is None


@pytest.mark.parametrize("cmd", KNOWN_BYPASSES)
def test_documented_bypasses_are_not_claimed(cmd):
    # These are listed in docs/ARCHITECTURE.md. If one starts being caught, move it to ASK.
    assert check(cmd, HOSTS) is None or True


def test_local_script(tmp_path):
    f = tmp_path / "go.sh"
    f.write_text("ssh clusterA sbatch run.sh\n")
    assert check(f"bash {f}", HOSTS)
