"""ファイルの文脈アクション(Issue #98)のテスト。"""
from hashi import fileactions
from hashi.fileactions import actions_for, build_command


def test_actions_for_matches_by_name_and_ext():
    assert any("up -d" in a.command for a in actions_for("docker-compose.yml"))
    assert any("up -d" in a.command for a in actions_for("Compose.YAML"))
    assert any("docker build" in a.command for a in actions_for("Dockerfile"))
    assert any("python3" in a.command for a in actions_for("app.py"))
    assert any("java -jar" in a.command for a in actions_for("server.JAR"))
    assert any("systemctl start" in a.command for a in actions_for("web.service"))
    assert actions_for("photo.png") == []
    assert actions_for("README") == []


def test_build_command_quotes_paths_and_resolves_builtins():
    act = fileactions.FileAction("実行", "bash {{path}}")
    cmd, missing = build_command(act, "/srv/my files/run's.sh")
    assert cmd == "bash '/srv/my files/run'\\''s.sh'"
    assert missing == []

    act2 = fileactions.FileAction("展開", "tar xzf {{path}} -C {{dir}}")
    cmd2, missing2 = build_command(act2, "/srv/app/dist.tgz")
    assert cmd2 == "tar xzf '/srv/app/dist.tgz' -C '/srv/app'"
    assert missing2 == []


def test_build_command_reports_user_variables():
    act = fileactions.FileAction("build", "docker build -t {{tag}} {{dir}}")
    cmd, missing = build_command(act, "/srv/proj/Dockerfile")
    assert "'/srv/proj'" in cmd
    assert missing == ["tag"]     # {{tag}} は入力ダイアログ行き


def test_service_action_uses_unit_name():
    acts = actions_for("myapp.service")
    start = next(a for a in acts if "start" in a.command)
    cmd, missing = build_command(start, "/etc/systemd/system/myapp.service")
    assert cmd == "sudo systemctl start 'myapp.service'"
    assert missing == []


def test_run_file_action_emits_to_terminal(qapp):
    """コマンドはターミナルへ入力されるだけで自動実行しない(#98)。"""
    from PySide6.QtWidgets import QWidget

    from hashi.filebrowser import SftpBrowser

    browser = SftpBrowser.__new__(SftpBrowser)
    QWidget.__init__(browser)
    statuses = []
    browser._on_status = statuses.append
    emitted = []
    browser.terminal_input.connect(emitted.append)

    act = fileactions.FileAction("Python で実行", "python3 {{path}}")
    browser._run_file_action(act, "/srv/app/main.py")

    assert emitted == ["python3 '/srv/app/main.py'"]
    assert all(not cmd.endswith("\n") for cmd in emitted)   # Enter は付けない
    assert any("Enter で実行" in s for s in statuses)
    browser.deleteLater()
