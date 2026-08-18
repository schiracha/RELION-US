"""
auth.py — optional password protection for the RELION-US interface itself
(not per-project, not per-RELION-account: one shared password gates the
whole broadcasting web app), plus the CLI ``Run-RelionUS`` calls to manage
it.

Why this exists: RELION-US binds 0.0.0.0 by default so it's reachable from
another machine (see main.py's module docstring) -- on a shared cluster or
lab network, that means anyone who can reach the port can open jobs, run
them, and delete run history, with no login at all. This module is a
deliberately simple deterrent against that, not a security system:

- No TLS is set up by this app, so the password (like everything else this
  app sends) crosses the network in plain text. That is an accepted
  trade-off, not an oversight -- ask before adding HTTPS here rather than
  assuming it's missing by mistake. If you need real confidentiality, put
  this behind a reverse proxy (nginx/Caddy) with TLS termination, or tunnel
  over SSH (`ssh -L 8420:localhost:8420 <host>`, already how the README
  suggests reaching a remote/HPC-hosted instance).
- One shared password, not per-user accounts. There is nothing here to
  audit "who did what" -- it only answers "did whoever's asking know the
  password".
- Turning it on/off and setting the password are both terminal-only (see
  the CLI at the bottom of this file, wired up as `Run-RelionUS
  --set-password` / `--enable-auth` / `--disable-auth` / `--auth-status`).
  There is deliberately no in-browser "change password" flow: anyone who can
  already reach a shell on the machine running the backend can read/edit
  project files anyway, so gating password changes behind browser auth
  would not add real protection, only friction.

Storage: `project_manager.config_root() / "auth.json"` -- per-*user*, like
the recent-projects cache, not per-project (the whole point is protecting
the app before it even shows a project). Only a salted hash is stored, never
the password itself, using stdlib `hashlib.pbkdf2_hmac` rather than pulling
in bcrypt/argon2 as a new dependency -- iteration count is tuned to still be
a real cost per guess, appropriate for "deter casual access to a lab
instrument," not for defending a password worth targeting with a GPU.

Sessions: a signed, stateless cookie (HMAC-SHA256 over an expiry timestamp,
keyed by a random secret stored alongside the password hash) rather than a
server-side session table -- it survives a backend restart, and rotating the
secret (done automatically on every password change) is what invalidates
every existing session at once.
"""
from __future__ import annotations

import getpass
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any

import project_manager

CONFIG_FILENAME = "auth.json"
COOKIE_NAME = "relion_us_session"
SESSION_LIFETIME_SECONDS = 30 * 24 * 60 * 60  # 30 days -- a lab instrument
# left logged in, not a banking site; re-entering a shared password every
# day would just train people to leave the tab open forever instead.
PBKDF2_ITERATIONS = 260_000  # ~OWASP's current floor for PBKDF2-SHA256
MIN_PASSWORD_LENGTH = 4  # a deterrent floor, not a strength policy -- see
# the module docstring for the threat model this is (and isn't) sized for.


def config_path() -> Path:
    return project_manager.config_root() / CONFIG_FILENAME


def _default_config() -> dict[str, Any]:
    return {"enabled": False, "password_hash": None, "salt": None, "session_secret": None}


def config_exists() -> bool:
    """Whether this machine has ever been asked about password protection --
    Run-RelionUS's first-run prompt uses this to ask only once."""
    return config_path().exists()


def load_config() -> dict[str, Any]:
    """Current config, tolerant of a missing or corrupt file (treated the
    same as project_manager.load_recent_projects treats its own cache: a
    problem reading *this* file should never be why the app won't start)."""
    p = config_path()
    if not p.exists():
        return _default_config()
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return _default_config()
    cfg = _default_config()
    cfg.update({k: data.get(k, v) for k, v in cfg.items()})
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2))
    # Holds a password hash and a session-signing secret -- neither is the
    # plaintext password, but there's no reason to leave it group/world
    # readable on a shared machine either. Best-effort: not every filesystem
    # (e.g. some network mounts) honours chmod, and that's not worth failing
    # startup over.
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    ).hex()


def set_password(password: str) -> None:
    """Stores a new password (as a salted hash) and rotates the session
    secret, so every session anywhere -- including one an attacker who'd
    guessed the *old* password is holding -- is invalidated at once."""
    cfg = load_config()
    salt = secrets.token_bytes(16)
    cfg["salt"] = salt.hex()
    cfg["password_hash"] = _hash_password(password, salt)
    cfg["session_secret"] = secrets.token_hex(32)
    save_config(cfg)


def verify_password(password: str, cfg: dict[str, Any] | None = None) -> bool:
    cfg = cfg if cfg is not None else load_config()
    if not cfg.get("password_hash") or not cfg.get("salt"):
        return False
    salt = bytes.fromhex(cfg["salt"])
    candidate = _hash_password(password, salt)
    return hmac.compare_digest(candidate, cfg["password_hash"])


def is_enabled(cfg: dict[str, Any] | None = None) -> bool:
    """Whether THIS run should require a login -- the persisted setting,
    unless Run-RelionUS's --auth/--no-auth overrode it for just this one
    process (RELION_US_FORCE_AUTH=1/0). Forcing "on" still requires a
    password to already be set: there's no password to require a login
    against otherwise, so an --auth with none set is treated as if it
    weren't passed rather than locking everyone out."""
    cfg = cfg if cfg is not None else load_config()
    has_password = bool(cfg.get("password_hash"))
    override = os.environ.get("RELION_US_FORCE_AUTH")
    if override is not None:
        if override in ("0", "false", "False", ""):
            return False
        return has_password
    return bool(cfg.get("enabled")) and has_password


def enable() -> None:
    cfg = load_config()
    if not cfg.get("password_hash"):
        raise RuntimeError(
            "No password is set yet -- run `Run-RelionUS --set-password` first."
        )
    cfg["enabled"] = True
    save_config(cfg)


def disable() -> None:
    cfg = load_config()
    cfg["enabled"] = False
    save_config(cfg)


# --- Sessions ---------------------------------------------------------------
# Stateless: the cookie IS the session, so a backend restart doesn't log
# everyone out and there is no session table to leak or clean up. Whoever
# holds a valid, unexpired token is treated as authenticated -- there is
# only ever one password, so there is nothing more specific to authenticate
# *as*.

def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def new_session_token(cfg: dict[str, Any] | None = None) -> str:
    cfg = cfg if cfg is not None else load_config()
    secret = cfg.get("session_secret") or ""
    expiry = str(int(time.time()) + SESSION_LIFETIME_SECONDS)
    return f"{expiry}.{_sign(expiry, secret)}"


def session_is_valid(token: str | None, cfg: dict[str, Any] | None = None) -> bool:
    if not token:
        return False
    cfg = cfg if cfg is not None else load_config()
    secret = cfg.get("session_secret") or ""
    if not secret:
        return False
    try:
        expiry, sig = token.split(".", 1)
    except ValueError:
        return False
    if not hmac.compare_digest(_sign(expiry, secret), sig):
        return False
    try:
        return int(expiry) > time.time()
    except ValueError:
        return False


# --- CLI (Run-RelionUS --set-password / --enable-auth / --disable-auth /
# --auth-status) --------------------------------------------------------

def _prompt_new_password() -> str | None:
    """Reads a new password twice (hidden input) and confirms they match.
    Returns None (without printing an error) on Ctrl-C/EOF, so callers can
    bail out quietly the way an interactive prompt is expected to."""
    try:
        while True:
            pw1 = getpass.getpass("New RELION-US password: ")
            if len(pw1) < MIN_PASSWORD_LENGTH:
                print(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
                continue
            pw2 = getpass.getpass("Confirm: ")
            if pw1 != pw2:
                print("Passwords didn't match -- try again.")
                continue
            return pw1
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def cli_set_password() -> int:
    pw = _prompt_new_password()
    if pw is None:
        print("Cancelled -- password unchanged.")
        return 1
    set_password(pw)
    cfg = load_config()
    print(f"Password set. ({config_path()})")
    if not cfg.get("enabled"):
        print("Protection is currently OFF -- run `Run-RelionUS --enable-auth` "
              "to require it, or start the server with --auth to turn it on "
              "for just that run.")
    return 0


def cli_enable() -> int:
    try:
        enable()
    except RuntimeError as exc:
        print(str(exc))
        return 1
    print("Password protection is now ON.")
    return 0


def cli_disable() -> int:
    had_password = bool(load_config().get("password_hash"))
    disable()
    if had_password:
        print("Password protection is now OFF. (The stored password is kept -- "
              "re-enable any time with `Run-RelionUS --enable-auth`.)")
    else:
        print("Password protection is now OFF.")
    return 0


def cli_status() -> int:
    cfg = load_config()
    has_pw = bool(cfg.get("password_hash"))
    print(f"Config file:  {config_path()}")
    print(f"Password set: {'yes' if has_pw else 'no'}")
    print(f"Protection:   {'ON' if is_enabled(cfg) else 'OFF'}")
    return 0


def cli_config_exists() -> int:
    """Silent -- exit code only. Run-RelionUS's first-run prompt uses this to
    decide whether to ask about password protection at all."""
    return 0 if config_exists() else 1


def main(argv: list[str]) -> int:
    commands = {
        "status": cli_status,
        "set-password": cli_set_password,
        "enable": cli_enable,
        "disable": cli_disable,
        "config-exists": cli_config_exists,
    }
    if len(argv) != 1 or argv[0] not in commands:
        print(f"Usage: {Path(sys.argv[0]).name} "
              "{status|set-password|enable|disable|config-exists}")
        return 2
    return commands[argv[0]]()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
