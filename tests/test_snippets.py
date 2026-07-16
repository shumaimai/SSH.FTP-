"""スニペットの永続化・変数置換のテスト。"""

from hashi.snippets import Snippet, SnippetStore, expand_snippet, find_variables


def test_find_variables_extracts_in_order_and_dedupes():
    body = "echo {{a}} {{b}} {{a}}"
    assert find_variables(body) == ["a", "b"]


def test_find_variables_allows_whitespace_around_name():
    body = "systemctl restart {{ service }}"
    assert find_variables(body) == ["service"]


def test_expand_snippet_replaces_variables():
    body = "systemctl restart {{service}}"
    assert expand_snippet(body, {"service": "nginx"}) == "systemctl restart nginx"


def test_expand_snippet_keeps_unknown_variables():
    body = "echo {{known}} {{unknown}}"
    assert expand_snippet(body, {"known": "x"}) == "echo x {{unknown}}"


def test_expand_snippet_handles_whitespace_variants():
    body = "systemctl restart {{ service }} now"
    assert expand_snippet(body, {"service": "nginx"}) == "systemctl restart nginx now"


def test_snippet_store_persistence(tmp_config):
    path = tmp_config / "snippets.json"
    store = SnippetStore(path=path)
    store.add(Snippet(name="hello", body="echo {{x}}", send_enter=False))
    store.add(Snippet(name="deploy", body="git pull", send_enter=True))

    store2 = SnippetStore(path=path)
    assert len(store2.snippets) == 2
    assert store2.snippets[0].name == "hello"
    assert store2.snippets[0].body == "echo {{x}}"
    assert store2.snippets[0].send_enter is False
    assert store2.snippets[1].name == "deploy"


def test_snippet_store_update_and_remove(tmp_config):
    store = SnippetStore(path=tmp_config / "snippets.json")
    store.add(Snippet(name="a", body="echo 1"))
    store.add(Snippet(name="b", body="echo 2"))

    store.update(0, Snippet(name="a", body="echo one"))
    assert store.snippets[0].body == "echo one"

    store.remove(0)
    assert [s.name for s in store.snippets] == ["b"]


def test_snippet_store_move(tmp_config):
    store = SnippetStore(path=tmp_config / "snippets.json")
    store.add(Snippet(name="a"))
    store.add(Snippet(name="b"))
    store.add(Snippet(name="c"))

    store.move_up(1)
    assert [s.name for s in store.snippets] == ["b", "a", "c"]

    store.move_down(0)
    assert [s.name for s in store.snippets] == ["a", "b", "c"]


def test_snippet_from_dict_ignores_unknown_keys():
    s = Snippet.from_dict({"name": "x", "body": "y", "extra": "ignored"})
    assert s.name == "x"
    assert s.body == "y"
    assert not hasattr(s, "extra")
