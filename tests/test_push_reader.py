import importlib.util
import os
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("push_reader", ROOT / "examples" / "push_reader.py")
pr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pr)


def test_payload_mapping():
    p = pr.to_payload(-1001234567890, 77, "Шукаю віллу", 1_790_000_000, 555)
    assert p == {"chat_ref": "-1001234567890", "message_id": 77, "text": "Шукаю віллу", "date": 1_790_000_000.0, "author_id": "555"}
    assert pr.to_payload(-1, 1, "x", 1, None)["author_id"] is None          # channel posts have no author


def test_reader_is_strictly_read_only_and_has_no_evasion_features():
    src = (ROOT / "examples" / "push_reader.py").read_text()
    code = re.sub(r'""".*?"""', "", src, flags=re.S)                         # ignore the docstring that documents the limits
    banned = ("send_message", "send_file", "forward_messages", "JoinChannelRequest", "ImportChatInvite", "AddChatUser",
              "SendReactionRequest", "edit_message", "delete_messages", "proxy", "rotate", "SessionString", "StringSession")
    assert not [w for w in banned if w in code]


def test_session_file_permissions_are_checked(tmp_path):
    f = tmp_path / "s.session"
    f.write_text("x"); f.chmod(0o644)
    assert pr.session_is_private(str(f)) is False
    f.chmod(0o600)
    assert pr.session_is_private(str(f)) is True
    assert pr.session_is_private(str(tmp_path / "missing")) is True
