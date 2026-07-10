import pytest


@pytest.mark.parametrize("text,expected", [
    ("[sudo] password for tester: ", "sudo"),
    ("tester@host's password: ", "password"),
    ("Password: ", "password"),
    ("Enter passphrase for key '/home/x/id_ed25519': ", "passphrase"),
])
def test_prompt_detected(qapp, text, expected):
    from hashi.terminal import TerminalWidget
    t = TerminalWidget()
    hits = []
    t.password_prompt.connect(lambda k: hits.append(k))
    t.screen.reset()
    t._on_data(text.encode())
    t._detect_password_prompt()
    assert hits == [expected]


@pytest.mark.parametrize("text", [
    "$ echo hello",
    "Verification code: ",     # 2FA はパスワードではないので反応しない
    "some normal output line",
])
def test_prompt_not_detected(qapp, text):
    from hashi.terminal import TerminalWidget
    t = TerminalWidget()
    hits = []
    t.password_prompt.connect(lambda k: hits.append(k))
    t.screen.reset()
    t._on_data(text.encode())
    t._detect_password_prompt()
    assert hits == []
