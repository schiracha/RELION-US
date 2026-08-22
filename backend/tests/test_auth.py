"""
Tests for auth.py — the optional password gate for the RELION-US interface
itself (see the module docstring there for the threat model this is, and
deliberately isn't, sized for): password hashing/verification, the
enabled/disabled toggle, session tokens, the RELION_US_FORCE_AUTH per-run
override Run-RelionUS's --auth/--no-auth set, and the terminal-only CLI
(Run-RelionUS --set-password / --enable-auth / --disable-auth / --auth-status).

The HTTP-level behavior (the actual redirect/401/login flow, the websocket
gate) is covered by test_auth.py's Playwright suite instead -- this file is
the unit layer underneath it: the hashing, the token, the config file.
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auth


@pytest.fixture
def auth_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return tmp_path


# --- config storage -----------------------------------------------------

def test_config_path_follows_xdg_config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert auth.config_path() == tmp_path / "cfg" / "relion_us" / "auth.json"


def test_no_config_file_yet_reads_as_disabled_with_no_password(auth_home):
    cfg = auth.load_config()
    assert cfg["enabled"] is False
    assert cfg["password_hash"] is None
    assert auth.is_enabled(cfg) is False


def test_corrupt_config_file_is_treated_as_default_not_an_error(auth_home):
    auth.config_path().parent.mkdir(parents=True)
    auth.config_path().write_text("not valid json {{{")
    cfg = auth.load_config()
    assert cfg == auth._default_config()


def test_config_file_is_written_with_restrictive_permissions(auth_home):
    auth.set_password("hunter22")
    mode = auth.config_path().stat().st_mode & 0o777
    assert mode == 0o600


# --- password hashing/verification --------------------------------------

def test_set_password_then_verify_round_trips(auth_home):
    auth.set_password("correct horse battery staple")
    assert auth.verify_password("correct horse battery staple") is True


def test_verify_rejects_wrong_password(auth_home):
    auth.set_password("correct horse battery staple")
    assert auth.verify_password("wrong") is False


def test_verify_with_no_password_set_is_always_false(auth_home):
    # Not an error, and importantly not "true because anything matches
    # nothing" -- an unconfigured instance must never authenticate anyone.
    assert auth.verify_password("anything") is False


def test_password_is_never_stored_in_the_clear(auth_home):
    auth.set_password("correct horse battery staple")
    raw = auth.config_path().read_text()
    assert "correct horse battery staple" not in raw


def test_same_password_gets_a_different_hash_each_time(auth_home):
    # Salted -- two users (or the same user setting the same password twice)
    # shouldn't produce comparable hashes.
    auth.set_password("samepassword")
    hash1 = auth.load_config()["password_hash"]
    auth.set_password("samepassword")
    hash2 = auth.load_config()["password_hash"]
    assert hash1 != hash2


# --- enable/disable -------------------------------------------------------

def test_enabling_without_a_password_raises(auth_home):
    with pytest.raises(RuntimeError):
        auth.enable()


def test_enable_requires_a_password_then_is_enabled(auth_home):
    auth.set_password("x123")
    auth.enable()
    assert auth.is_enabled() is True


def test_setting_a_password_does_not_itself_enable_it(auth_home):
    auth.set_password("x123")
    assert auth.is_enabled() is False


def test_disable_keeps_the_password_hash(auth_home):
    auth.set_password("x123")
    auth.enable()
    auth.disable()
    cfg = auth.load_config()
    assert cfg["enabled"] is False
    assert cfg["password_hash"] is not None
    assert auth.verify_password("x123", cfg) is True


# --- RELION_US_FORCE_AUTH (Run-RelionUS --auth / --no-auth) ---------------

def test_force_auth_0_disables_even_if_persisted_enabled(auth_home, monkeypatch):
    auth.set_password("x123")
    auth.enable()
    monkeypatch.setenv("RELION_US_FORCE_AUTH", "0")
    assert auth.is_enabled() is False


def test_force_auth_1_enables_if_a_password_exists(auth_home, monkeypatch):
    auth.set_password("x123")
    # Deliberately not calling enable() -- the whole point is the override.
    monkeypatch.setenv("RELION_US_FORCE_AUTH", "1")
    assert auth.is_enabled() is True


def test_force_auth_1_is_a_no_op_without_a_password(auth_home, monkeypatch):
    # Can't force a login requirement into existence with nothing to check
    # a guess against -- this must fall back to disabled, not raise or
    # lock everyone out.
    monkeypatch.setenv("RELION_US_FORCE_AUTH", "1")
    assert auth.is_enabled() is False


# --- sessions ---------------------------------------------------------------

def test_fresh_session_token_is_valid(auth_home):
    auth.set_password("x123")
    cfg = auth.load_config()
    token = auth.new_session_token(cfg)
    assert auth.session_is_valid(token, cfg) is True


def test_no_token_is_invalid(auth_home):
    auth.set_password("x123")
    assert auth.session_is_valid(None) is False
    assert auth.session_is_valid("") is False


def test_malformed_token_is_invalid(auth_home):
    auth.set_password("x123")
    cfg = auth.load_config()
    assert auth.session_is_valid("not.a.valid.token.shape.but.has.dots", cfg) is False
    assert auth.session_is_valid("nodothere", cfg) is False


def test_tampered_token_is_invalid(auth_home):
    auth.set_password("x123")
    cfg = auth.load_config()
    token = auth.new_session_token(cfg)
    expiry, sig = token.split(".", 1)
    tampered = f"{int(expiry) + 1_000_000}.{sig}"  # extended expiry, stale signature
    assert auth.session_is_valid(tampered, cfg) is False


def test_expired_token_is_invalid(auth_home):
    auth.set_password("x123")
    cfg = auth.load_config()
    expired_expiry = str(int(time.time()) - 10)
    sig = auth._sign(expired_expiry, cfg["session_secret"])
    assert auth.session_is_valid(f"{expired_expiry}.{sig}", cfg) is False


def test_changing_password_invalidates_existing_sessions(auth_home):
    auth.set_password("first")
    cfg1 = auth.load_config()
    token = auth.new_session_token(cfg1)
    assert auth.session_is_valid(token, auth.load_config()) is True

    auth.set_password("second")
    cfg2 = auth.load_config()
    assert cfg2["session_secret"] != cfg1["session_secret"]
    assert auth.session_is_valid(token, cfg2) is False


# --- CLI ---------------------------------------------------------------

def test_cli_set_password_prompts_twice_and_stores_it(auth_home, monkeypatch, capsys):
    answers = iter(["newpassword", "newpassword"])
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt="": next(answers))
    rc = auth.cli_set_password()
    assert rc == 0
    assert auth.verify_password("newpassword") is True


def test_cli_set_password_retries_on_mismatch(auth_home, monkeypatch):
    answers = iter(["passwordone", "passwordtwo", "passwordone", "passwordone"])
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt="": next(answers))
    rc = auth.cli_set_password()
    assert rc == 0
    assert auth.verify_password("passwordone") is True


def test_cli_set_password_rejects_too_short(auth_home, monkeypatch):
    answers = iter(["ab", "okpassword", "okpassword"])
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt="": next(answers))
    rc = auth.cli_set_password()
    assert rc == 0
    assert auth.verify_password("okpassword") is True


def test_cli_set_password_cancelled_leaves_password_unset(auth_home, monkeypatch):
    def raise_eof(prompt=""):
        raise EOFError
    monkeypatch.setattr(auth.getpass, "getpass", raise_eof)
    rc = auth.cli_set_password()
    assert rc == 1
    assert auth.load_config()["password_hash"] is None


def test_cli_enable_without_password_fails(auth_home, capsys):
    rc = auth.cli_enable()
    assert rc == 1
    assert auth.is_enabled() is False


def test_cli_enable_then_disable(auth_home):
    auth.set_password("x123")
    assert auth.cli_enable() == 0
    assert auth.is_enabled() is True
    assert auth.cli_disable() == 0
    assert auth.is_enabled() is False


def test_cli_is_enabled_reflects_whether_protection_is_currently_on(auth_home):
    # No config at all yet -- Run-RelionUS's startup prompt should still ask.
    assert auth.cli_is_enabled() == 1
    auth.set_password("x123")
    # A password alone doesn't count as "enabled" -- still asks.
    assert auth.cli_is_enabled() == 1
    auth.enable()
    assert auth.cli_is_enabled() == 0
    auth.disable()
    # Declining/disabling doesn't opt you out of being asked again.
    assert auth.cli_is_enabled() == 1


def test_cli_main_dispatches_and_rejects_unknown_commands(auth_home, capsys):
    assert auth.main(["status"]) == 0
    assert auth.main(["not-a-real-command"]) == 2
    assert auth.main([]) == 2
