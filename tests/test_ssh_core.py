"""ssh_core.py のユニットテスト。"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from hashi.config import Profile
from hashi.ssh_core import SshSession


def test_security_summary_reports_negotiated_cipher():
    """接続確立後の transport からネゴシエート済み暗号 / MAC をまとめる(#113)。"""
    session = SshSession(Profile())
    # transport 未接続なら空文字
    assert session.security_summary() == ""

    t = MagicMock()
    t.is_active.return_value = True
    t.remote_cipher = "aes256-ctr"
    t.local_cipher = "aes256-ctr"
    t.remote_mac = "hmac-sha2-256"
    t.local_mac = "hmac-sha2-256"
    session.transport = t
    assert session.security_summary() == "aes256-ctr / hmac-sha2-256"

    # GCM 系は MAC を内包するので省く
    t.remote_cipher = "aes256-gcm@openssh.com"
    t.local_cipher = "aes256-gcm@openssh.com"
    assert session.security_summary() == "aes256-gcm@openssh.com"

    # 非アクティブなら空
    t.is_active.return_value = False
    assert session.security_summary() == ""


def test_open_shell_attaches_agent_request_handler_when_enabled():
    """agent_forwarding=True のとき、シェルチャネルに AgentRequestHandler を仕掛け保持する。"""
    session = SshSession(Profile(agent_forwarding=True))
    fake_ch = MagicMock()
    fake_transport = MagicMock()
    fake_transport.open_session.return_value = fake_ch
    session.transport = fake_transport

    with patch("paramiko.agent.AgentRequestHandler") as MockHandler:
        handler = MockHandler.return_value
        ch = session.open_shell()

    assert ch is fake_ch
    MockHandler.assert_called_once_with(fake_ch)
    assert session._agent_handlers == [handler]
    assert ch._hashi_agent_handler is handler


def test_open_shell_does_not_attach_agent_handler_when_disabled():
    """agent_forwarding=False のとき、AgentRequestHandler は作られない。"""
    session = SshSession(Profile(agent_forwarding=False))
    fake_ch = MagicMock()
    fake_transport = MagicMock()
    fake_transport.open_session.return_value = fake_ch
    session.transport = fake_transport

    with patch("paramiko.agent.AgentRequestHandler") as MockHandler:
        session.open_shell()

    MockHandler.assert_not_called()
    assert session._agent_handlers == []


def test_close_closes_agent_handlers_and_transport():
    """SshSession.close は保持している AgentRequestHandler を先に閉じる。"""
    session = SshSession(Profile())
    fake_transport = MagicMock()
    session.transport = fake_transport

    handler = MagicMock()
    session._agent_handlers.append(handler)

    session.close()

    handler.close.assert_called_once()
    fake_transport.close.assert_called_once()
    assert session._agent_handlers == []
    assert session.transport is None


def test_close_is_safe_with_partially_broken_handler():
    """AgentRequestHandler の close で例外が出ても transport.close は続行する。"""
    session = SshSession(Profile())
    fake_transport = MagicMock()
    session.transport = fake_transport

    handler = MagicMock()
    handler.close.side_effect = RuntimeError("boom")
    session._agent_handlers.append(handler)

    session.close()

    handler.close.assert_called_once()
    fake_transport.close.assert_called_once()
    assert session._agent_handlers == []
