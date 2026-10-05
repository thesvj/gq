import shutil
import subprocess
from pathlib import Path

import pytest

R = Path(__file__).resolve().parents[1] / "gq" / "remote_files"
SCRIPTS = [R / "gqr", R / "agentd", R / "rc", R / "shim" / "sbatch", R / "shim" / "salloc"]


@pytest.mark.parametrize("p", SCRIPTS, ids=lambda p: p.name)
def test_bash_syntax(p):
    subprocess.run(["bash", "-n", str(p)], check=True)


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="shellcheck not installed")
@pytest.mark.parametrize("p", SCRIPTS, ids=lambda p: p.name)
def test_shellcheck(p):
    subprocess.run(["shellcheck", "-S", "error", "-s", "bash", str(p)], check=True)


@pytest.mark.skipif(not shutil.which("vermin"), reason="vermin not installed")
def test_snap_runs_on_python36():
    out = subprocess.run(["vermin", "--target=3.6-", "--no-tips", "--violations", str(R / "snap.py")],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr


def test_login_node_scripts_never_compute():
    """gqr/snap.py run on the login node: they may only query slurm, manage tmux and touch ~/.gq."""
    text = (R / "gqr").read_text() + (R / "snap.py").read_text()
    for bad in ("python train", "torch", "nvidia-smi", "sbatch "):
        assert bad not in text, bad


def test_shim_blocks_in_agent_runs(tmp_path):
    out = subprocess.run(["bash", str(R / "shim" / "sbatch"), "--wrap=x"], capture_output=True, text=True,
                         env={"GQ_AGENT": "1", "PATH": "/usr/bin:/bin"})
    assert out.returncode == 97 and "blocked" in out.stderr


def test_new_pane_never_exports_an_empty_slurm_conf():
    """Regression (precision): exporting SLURM_CONF="" makes srun read the path "" ("cannot stat file :")."""
    import subprocess
    text = (R / "gqr").read_text()
    assert 'SLURM_CONF=$(printf %q "${SLURM_CONF:-}")' not in text
    line = next(l for l in text.splitlines() if l.strip().startswith('envs="export PATH='))
    for conf, want in (("", "unset"), ("/etc/slurm/slurm.conf", "/etc/slurm/slurm.conf")):
        script = (f'SLURM_CONF={conf!r}; [ -z "$SLURM_CONF" ] && unset SLURM_CONF; {line.strip()}; '
                  'eval "$envs"; [ -z "${SLURM_CONF:-}" ] && unset SLURM_CONF; echo "${SLURM_CONF-unset}"')
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}).stdout.strip()
        assert out == want, (conf, out)
