"""setup_launcher.py — fresh-terminal spawn for the interactive wizard."""
from buddhi_review import setup_launcher


def _which_none(_):
    return None


def _which_all(_):
    return "/usr/bin/term"


def test_detect_strategy_macos():
    assert setup_launcher.detect_strategy("Darwin", _which_none, environ={}) == "macos"


def test_detect_strategy_windows():
    assert setup_launcher.detect_strategy("Windows", _which_none, environ={}) == "windows"


def test_detect_strategy_linux_picks_first_terminal():
    def which(name):
        return "/usr/bin/konsole" if name == "konsole" else None
    strat = setup_launcher.detect_strategy("Linux", which, environ={"DISPLAY": ":0"})
    assert strat == "konsole"


def test_detect_strategy_headless_linux_prints():
    assert setup_launcher.detect_strategy("Linux", _which_all, environ={}) == "print"


def test_detect_strategy_ssh_macos_prints():
    env = {"SSH_CONNECTION": "1.2.3.4 22", "DISPLAY": "localhost:10.0"}
    assert setup_launcher.detect_strategy("Darwin", _which_all, environ=env) == "print"


def test_detect_strategy_ssh_linux_forwarded_x11_spawns():
    env = {"SSH_CONNECTION": "x", "DISPLAY": "localhost:10.0"}
    def which(name):
        return "/usr/bin/xterm" if name == "xterm" else None
    assert setup_launcher.detect_strategy("Linux", which, environ=env) == "xterm"


def test_spawn_wizard_macos_writes_command_file_and_opens():
    opened = []
    written = []

    def popen(argv):
        opened.append(argv)

    def write_cmd(_unused, shell_cmd, command_file_dir, *, name="buddhi-review-setup"):
        written.append(shell_cmd)
        return "/tmp/x.command"

    result = setup_launcher.spawn_wizard(
        system="Darwin", which=_which_none, popen=popen, environ={},
        write_command_file=write_cmd, python_bin="/usr/bin/python3")
    assert result["spawned"] is True
    assert result["strategy"] == "macos"
    assert opened and opened[0][0] == "open"
    # The command runs the installed module, not a script path.
    assert "-m buddhi_review setup" in written[0]
    # The window stays open after the wizard exits so the summary is readable.
    assert "exec bash" in written[0]


def test_spawn_wizard_headless_returns_not_spawned():
    import io
    buf = io.StringIO()
    result = setup_launcher.spawn_wizard(
        system="Linux", which=_which_none, popen=lambda *a: None, environ={},
        stream=buf, python_bin="/usr/bin/python3")
    assert result["spawned"] is False
    assert result["strategy"] == "print"
    assert "-m buddhi_review setup" in buf.getvalue()


def test_spawn_wizard_forwards_repo_arg():
    written = []

    def write_cmd(_unused, shell_cmd, command_file_dir, *, name="buddhi-review-setup"):
        written.append(shell_cmd)
        return "/tmp/x.command"

    setup_launcher.spawn_wizard(
        system="Darwin", which=_which_none, popen=lambda *a: None, environ={},
        write_command_file=write_cmd, python_bin="/usr/bin/python3",
        wizard_args=["--repo", "acme/widgets"])
    assert "--repo acme/widgets" in written[0]


def test_spawn_command_macos_keeps_window_open():
    written = []

    def write_cmd(_unused, shell_cmd, command_file_dir, *, name="command"):
        written.append((name, shell_cmd))
        return "/tmp/y.command"

    result = setup_launcher.spawn_command(
        "claude setup-token", label="claude-setup-token", system="Darwin",
        which=_which_none, popen=lambda *a: None, environ={}, write_command_file=write_cmd)
    assert result["spawned"] is True
    name, cmd = written[0]
    assert name == "buddhi-claude-setup-token"
    assert "claude setup-token" in cmd
