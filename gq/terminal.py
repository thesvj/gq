"""Open a new terminal window running a command — works with whatever terminal the user has.

Order: config `terminal =` (a command template with {cmd}) > $GQ_TERMINAL > xdg-terminal-exec >
known emulators > macOS Terminal.app. Used only for GUI launches (keyboard shortcut / app icon);
from an existing terminal gq simply runs in place.
"""
import os
import shlex
import shutil
import subprocess
import sys

# emulator -> argv prefix that runs the following argv as a command
KNOWN = [
    ("xdg-terminal-exec", []),
    ("ghostty", ["-e"]), ("kitty", []), ("wezterm", ["start", "--"]), ("alacritty", ["-e"]),
    ("foot", []), ("ptyxis", ["--"]), ("gnome-terminal", ["--"]), ("kgx", ["-e"]),
    ("konsole", ["-e"]), ("xfce4-terminal", ["-x"]), ("tilix", ["-e"]), ("terminator", ["-x"]),
    ("st", ["-e"]), ("urxvt", ["-e"]), ("xterm", ["-e"]),
]


def argv_for(cmd, template=None):
    """cmd: list[str]. template: e.g. 'kitty --single-instance {cmd}' -> argv list."""
    template = template or os.environ.get("GQ_TERMINAL")
    if template:
        joined = " ".join(shlex.quote(c) for c in cmd)
        return shlex.split(template.replace("{cmd}", joined)) if "{cmd}" in template else shlex.split(template) + cmd
    if sys.platform == "darwin":
        script = " ".join(shlex.quote(c) for c in cmd).replace("\\\\", "\\\\\\\\").replace('"', '\\\\"')
        return ["osascript", "-e", f'tell application "Terminal" to do script "{script}"',
                "-e", 'tell application "Terminal" to activate']
    for name, prefix in KNOWN:
        if shutil.which(name):
            return [name, *prefix, *cmd]
    raise RuntimeError("no terminal emulator found; set `terminal = <cmd> {cmd}` in [gq] of config.ini")


def open_window(cmd, template=None):
    subprocess.Popen(argv_for(cmd, template), start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
