import logging

from olian.app import redact


def test_tokens_never_reach_logs(core, caplog):
    tok = "bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
    assert tok not in redact(f"GET https://api.telegram.org/{tok}/getUpdates")
    assert "olp_abcdefghijklmnopqrstuvwxyz0123" not in redact("token olp_abcdefghijklmnopqrstuvwxyz0123")
    assert "SECRETVALUE12345" not in redact("Authorization: Bearer SECRETVALUE12345")
    with caplog.at_level(logging.INFO, logger="olian.test"):
        logging.getLogger("olian.test").info("calling %s", f"https://api.telegram.org/{tok}/sendMessage")
    assert "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw" not in caplog.text


def test_pii_salt_is_stable_and_private(core):
    a, b = core.s.pii_salt(), core.s.pii_salt()
    assert a == b and len(a) >= 32
    assert (core.s.data_dir / ".pii_salt").stat().st_mode & 0o077 == 0
