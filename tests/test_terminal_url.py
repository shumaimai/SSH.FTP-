"""ターミナルの URL 検出テスト (Issue #103)。

描画・マウスクリックは Qt を絡めるが、URL 検出ロジック自体は
offscreen な QApplication 上で動作する TerminalWidget を使う。
"""
import pytest


@pytest.fixture()
def term(qapp):
    from hashi.terminal import TerminalWidget

    t = TerminalWidget()
    t.screen.reset()
    t._cols, t._rows = 80, 24
    return t


def _feed(term, text: str):
    """UTF-8 テキストを画面へ書き込む。"""
    term.stream.feed(text.encode("utf-8"))


def test_detect_simple_url(term):
    _feed(term, "Check https://example.com here")
    ranges = term._parse_urls(0)
    assert len(ranges) == 1
    sc, ec, url = ranges[0]
    assert url == "https://example.com"
    assert term._url_at(0, sc)
    assert term._url_at(0, ec - 1)
    assert term._url_at(0, sc - 1) is None
    assert term._url_at(0, ec) is None


def test_trim_trailing_punctuation(term):
    _feed(term, "See https://example.com. , or https://example.com; end")
    ranges = term._parse_urls(0)
    assert len(ranges) == 2
    assert ranges[0][2] == "https://example.com"
    assert ranges[1][2] == "https://example.com"


def test_keep_query_and_fragment(term):
    _feed(term, "curl https://example.com/path?a=1#frag")
    ranges = term._parse_urls(0)
    assert len(ranges) == 1
    assert ranges[0][2] == "https://example.com/path?a=1#frag"


def test_multiple_urls_on_same_line(term):
    _feed(term, "https://a.com and https://b.com/path")
    ranges = term._parse_urls(0)
    assert [r[2] for r in ranges] == ["https://a.com", "https://b.com/path"]


def test_no_url(term):
    _feed(term, "no link here")
    assert term._parse_urls(0) == []


def test_url_at_considers_col(term):
    _feed(term, "abc https://example.com def")
    for col in range(4, 23):
        assert term._url_at(0, col) == "https://example.com"
    assert term._url_at(0, 3) is None
    assert term._url_at(0, 23) is None
