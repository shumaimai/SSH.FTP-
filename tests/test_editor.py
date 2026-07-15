"""editor.py のテスト(Issue #7)。言語判定・ハイライト・検索・保存フロー。"""
import pytest
from PySide6.QtGui import QTextDocument


@pytest.mark.parametrize("path,expected", [
    ("/srv/app/main.py", "python"),
    ("/x/win.PYW", "python"),
    ("/x/kernel.c", "c"),
    ("/x/lib.rs", "c"),
    ("/x/Main.java", "c"),
    ("/x/app.tsx", "js"),
    ("/x/package.json", "js"),
    ("/home/tester/.bashrc", "shell"),
    ("/x/deploy.sh", "shell"),
    ("/etc/nginx/nginx.conf", "conf"),
    ("/x/pyproject.toml", "conf"),
    ("/x/README.md", "plain"),
    ("/x/noext", "plain"),
])
def test_lang_for(path, expected):
    from hashi.editor import _lang_for
    assert _lang_for(path) == expected


_SAMPLES = {
    "python": 'def f(x):\n    # comment\n    return "text"\n',
    "c": '/* block\n   comment */\nint main() { return 0; } // eol\n',
    "js": 'const f = (x) => { return `t`; } // c\n/* b */\n',
    "shell": 'if [ -f x ]; then\n  echo "hi" # c\nfi\n',
    "conf": '[section]\nkey = value  # c\n',
    "plain": 'ただのテキスト\n',
}


@pytest.mark.parametrize("lang", sorted(_SAMPLES))
def test_highlighter_smoke(qapp, lang):
    """各言語のハイライトが例外なく走る(複数行ブロックコメント含む)。"""
    from hashi.editor import Highlighter
    doc = QTextDocument()
    hl = Highlighter(doc, lang)
    doc.setPlainText(_SAMPLES[lang])
    hl.rehighlight()  # 明示的に全行を通す


def test_code_edit_line_number_width_grows(qapp):
    from hashi.editor import CodeEdit
    e = CodeEdit()
    e.setPlainText("x")
    w_small = e.line_number_width()
    e.setPlainText("\n" * 9999)
    assert e.line_number_width() > w_small


class _FakeSettings:
    _d = {"editor_font_size": 12, "editor_tab_width": 4}

    def get(self, key):
        return self._d[key]


@pytest.fixture()
def editor_window(qapp, tmp_path):
    from hashi.editor import EditorWindow
    p = tmp_path / "sample.py"
    p.write_text("alpha\nbeta\nalpha tail\n", encoding="utf-8")
    calls = []

    def save_cb(remote, local, done):
        calls.append((remote, local, done))

    w = EditorWindow("/srv/sample.py", str(p), save_cb, _FakeSettings())
    w._calls = calls
    yield w
    w.editor.document().setModified(False)  # closeEvent の確認ダイアログ回避
    w.close()


def test_find_forward_and_wrap(editor_window):
    """前方検索でヒットを辿り、末尾まで来たら先頭へ回り込む。"""
    w = editor_window
    w.find_edit.setText("alpha")
    w._find(True)
    first = w.editor.textCursor().selectionStart()
    w._find(True)
    second = w.editor.textCursor().selectionStart()
    assert second > first
    w._find(True)  # もうヒットが無い → 先頭へ回り込み
    assert w.editor.textCursor().selectionStart() == first


def test_find_backward(editor_window):
    w = editor_window
    w.find_edit.setText("alpha")
    w._find(False)  # 末尾へ回り込んで最後のヒット
    assert w.editor.textCursor().selectionStart() > 0


def test_save_writes_local_and_calls_callback(editor_window, tmp_path):
    """save() はローカル一時ファイルへ書いてからアップロード用コールバックを呼ぶ。"""
    w = editor_window
    w.editor.selectAll()
    w.editor.insertPlainText("changed body\n")  # setPlainText は modified を立てない
    assert w.editor.document().isModified()
    w.save()
    assert (tmp_path / "sample.py").read_text(encoding="utf-8") == "changed body\n"
    assert len(w._calls) == 1
    remote, local, done = w._calls[0]
    assert remote == "/srv/sample.py"
    # 保存中の再入は無視される
    w.save()
    assert len(w._calls) == 1
    # アップロード完了 → modified フラグが下りる
    done(True, "")
    assert not w.editor.document().isModified()


def test_save_failure_keeps_modified(editor_window):
    w = editor_window
    w.editor.selectAll()
    w.editor.insertPlainText("v2\n")
    w.save()
    _, _, done = w._calls[0]
    # QMessageBox を出さないよう差し替え
    from unittest.mock import patch
    with patch("hashi.editor.QMessageBox.warning"):
        done(False, "permission denied")
    assert w.editor.document().isModified()


def test_find_with_empty_query_is_noop(editor_window):
    """検索クエリが空でもクラッシュせず何もしない。"""
    w = editor_window
    w.find_edit.setText("")
    before = w.editor.textCursor().position()
    w._find(True)
    assert w.editor.textCursor().position() == before


def test_find_miss_leaves_no_selection(editor_window):
    w = editor_window
    w.find_edit.setText("does-not-exist-xyz")
    w._find(True)
    assert not w.editor.textCursor().hasSelection()


def test_update_title_reflects_modified(editor_window):
    w = editor_window
    w.editor.document().setModified(False)
    w._update_title()
    assert not w.windowTitle().startswith("●")
    w.editor.document().setModified(True)
    w._update_title()
    assert w.windowTitle().startswith("●")
    assert "sample.py" in w.windowTitle()


def test_cursor_status_is_one_based(editor_window):
    from PySide6.QtGui import QTextCursor
    w = editor_window
    cur = w.editor.textCursor()
    cur.movePosition(QTextCursor.Start)
    w.editor.setTextCursor(cur)
    w._update_cursor_status()
    msg = w.statusBar().currentMessage()
    assert "行 1" in msg and "列 1" in msg


def test_lang_for_expanded_extensions():
    """Issue #64: 追加拡張子の言語判定。"""
    from hashi.editor import _lang_for

    assert _lang_for("Main.kt") == "c"
    assert _lang_for("app.swift") == "c"
    assert _lang_for("App.vue") == "js"
    assert _lang_for("deploy.ps1") == "shell"
    assert _lang_for("analysis.R") == "shell"
    assert _lang_for(".editorconfig") == "conf"
    assert _lang_for("app.properties") == "conf"
    assert _lang_for("readme.unknownext") == "plain"
