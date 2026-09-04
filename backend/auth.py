"""
auth.py — optional password protection for the RELION-US interface itself
(not per-project, not per-RELION-account: one shared password gates the
whole broadcasting web app), plus the CLI ``Run-RelionUS`` calls to manage
it.

Why this exists: RELION-US binds 127.0.0.1 by default (see main.py's module
docstring) specifically so it is NOT reachable from another machine without
deliberately opting in, via `--host 0.0.0.0` or an SSH tunnel -- but anyone
who does open it up that way, or who shares a login node where other users
can already reach localhost, gets no login at all otherwise. This module is
a deliberately simple deterrent against that, not a security system:

- **TLS is supported and is what makes this real.** `Run-RelionUS --tls`
  serves HTTPS directly (uvicorn's own SSL support), and `--make-cert`
  generates a self-signed certificate if you don't have a real one. Without
  TLS the password -- like everything else this app sends -- crosses the
  network in plain text, and no amount of hashing at rest helps, because the
  attacker reads the password itself off the wire. Run this over HTTPS, an
  SSH tunnel, or a TLS-terminating reverse proxy any time it is reachable
  from another machine.
- A self-signed certificate encrypts exactly as well as a CA-issued one; what
  it does not do is prove *which* server you reached, so the browser shows a
  one-time warning and an active man-in-the-middle is not ruled out on a
  hostile network. Pin the fingerprint `--make-cert` prints, or use a real
  certificate, if that matters where you are.
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
the app before it even shows a project). The file is created 0600 and never
holds the password itself, only a salted hash.

Hashing is stdlib `hashlib.scrypt` (RFC 7914) at n=2**15, r=8, p=1 -- the
parameters RFC 7914 itself gives for interactive logins, ~32 MB and ~0.12 s
per guess on a normal machine. scrypt is *memory*-hard, which is the
property that makes bulk GPU/ASIC guessing expensive rather than merely
slow, and it needs no new dependency (no bcrypt, no argon2). Hashes written
by the older PBKDF2-HMAC-SHA256 scheme are still verified, and are silently
re-hashed to scrypt on the next successful login (see needs_rehash /
upgrade_hash_if_needed), so an existing install upgrades itself without
anyone having to reset a password.

Online guessing is bounded separately, by a lockout (see
login_attempt_allowed): the KDF cost only prices *offline* guessing against
a stolen auth.json, and 0.12 s per try is no obstacle at all to a script
hammering the login endpoint.

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
import socket
import subprocess
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

# --- Password hashing -------------------------------------------------------
# scrypt (RFC 7914) at the RFC's own "interactive login" parameters. Memory-
# hardness is the point: n=2**15/r=8 forces ~32 MB of working memory per
# guess, which is what makes a GPU or ASIC farm expensive rather than just
# a bit slower. OpenSSL refuses scrypt above a 32 MB default unless maxmem
# is raised explicitly, so SCRYPT_MAXMEM is passed on every call -- without
# it hashlib.scrypt raises "memory limit exceeded" at exactly these params.
KDF_SCRYPT = "scrypt"
KDF_PBKDF2 = "pbkdf2_sha256"
CURRENT_KDF = KDF_SCRYPT
SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 128 * SCRYPT_N * SCRYPT_R * 2   # 2x the requirement, headroom
PBKDF2_ITERATIONS = 260_000  # legacy only -- still VERIFIED, never written
MIN_PASSWORD_LENGTH = 8  # see _password_complaint for what else is refused

# --- Online guessing --------------------------------------------------------
# The KDF above prices OFFLINE guessing (someone who stole auth.json). It does
# nothing against a script POSTing /api/auth/login in a loop, where 0.12s per
# try is no obstacle. This lockout is that second, separate control.
#
# Kept in memory rather than persisted: a restart clears it, but restarting
# the backend already requires a shell on this machine, and at that point the
# attacker can read auth.json directly -- so persisting would add I/O on every
# failed login to defend a case that is already lost.
LOGIN_MAX_FAILURES = 5          # consecutive failures before the door shuts
LOGIN_FAILURE_WINDOW = 300.0    # ...counted over this many seconds
LOGIN_LOCKOUT_SECONDS = 300.0   # ...and then locked out for this long


def config_path() -> Path:
    return project_manager.config_root() / CONFIG_FILENAME


def _default_config() -> dict[str, Any]:
    return {
        "enabled": False,
        "password_hash": None,
        "salt": None,
        "session_secret": None,
        # Which KDF produced password_hash. Absent in files written before
        # scrypt existed here, which is exactly what makes them PBKDF2 --
        # see load_config's own back-fill and needs_rehash.
        "kdf": None,
        # Paths to the TLS certificate/key --make-cert generated (or that
        # --tls-cert/--tls-key were last pointed at), so `--tls` alone can
        # find them on later runs. Not secret; the KEY FILE they name is.
        "tls_cert": None,
        "tls_key": None,
    }


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
    if cfg.get("password_hash") and not cfg.get("kdf"):
        # Written before this module had more than one KDF, so it can only
        # be the PBKDF2 one. Named explicitly here so every later check can
        # just read cfg["kdf"] instead of re-deriving "no key means old".
        cfg["kdf"] = KDF_PBKDF2
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    """Write auth.json 0600-from-creation, atomically.

    Two things this deliberately does NOT do, both of which the previous
    version did:

    * `write_text()` then `chmod(0600)` creates the file at the umask's
      permissions first and tightens them a moment later. On a shared login
      node -- the exact machine this feature exists for -- that is a real
      window in which another user can read the session-signing secret, and
      winning it needs nothing cleverer than a loop. `os.open(..., 0o600)`
      passes the mode to the syscall that creates the file, so the file never
      exists at wider permissions.
    * Writing in place truncates the real file before the new content lands,
      so a crash mid-write leaves an empty or half-written auth.json --
      which load_config would read as "no password set", silently turning
      protection off. Writing a temp file in the same directory and
      os.replace()-ing it is atomic on POSIX: readers see either the old
      file or the new one, never a partial.
    """
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".tmp{os.getpid()}")
    payload = json.dumps(cfg, indent=2)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        # Never leave a stray .tmp holding the session secret behind.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _hash_password(password: str, salt: bytes, kdf: str = CURRENT_KDF) -> str:
    """One password + salt -> hex digest, under the named KDF.

    `kdf` is a parameter rather than always CURRENT_KDF because verifying an
    existing hash has to use whichever function actually produced it; only
    set_password/upgrade_hash_if_needed get to choose."""
    pw = password.encode("utf-8")
    if kdf == KDF_SCRYPT:
        return hashlib.scrypt(
            pw, salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
            maxmem=SCRYPT_MAXMEM, dklen=32,
        ).hex()
    if kdf == KDF_PBKDF2:
        return hashlib.pbkdf2_hmac("sha256", pw, salt, PBKDF2_ITERATIONS).hex()
    raise ValueError(f"unknown KDF: {kdf!r}")


def _password_complaint(password: str) -> str | None:
    """Why this password is refused, or None if it's acceptable.

    Deliberately a floor, not a strength meter: length is the only property
    that reliably predicts guessing cost, and composition rules ("must have a
    digit and a symbol") mostly push people toward `Password1!` -- which is
    in every cracking wordlist -- while doing nothing about length. So this
    checks length, rejects the handful of passwords that are literally the
    first thing anyone tries, and otherwise gets out of the way. Matches
    NIST SP 800-63B's guidance (long minimum, screen against known-common
    values, no composition rules, no forced rotation)."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if password.lower() in _COMMON_PASSWORDS:
        return "That is one of the most commonly guessed passwords -- pick another."
    if len(set(password)) < 3:
        return "Password is too repetitive (fewer than 3 distinct characters)."
    return None


# Not a wordlist -- just the handful an unattended script tries in its first
# second. A real wordlist belongs behind the lockout, not in this file.
_COMMON_PASSWORDS = frozenset({
    "password", "password1", "password123", "12345678", "123456789",
    "1234567890", "qwertyui", "qwerty123", "letmein1", "iloveyou",
    "admin123", "welcome1", "abc12345", "relion", "relionus", "relion-us",
    "cryosparc", "changeme", "P@ssw0rd".lower(),
})


def set_password(password: str) -> None:
    """Stores a new password (as a salted hash) and rotates the session
    secret, so every session anywhere -- including one an attacker who'd
    guessed the *old* password is holding -- is invalidated at once.

    Raises ValueError if the password is refused (see _password_complaint);
    callers surface the message rather than storing something unusable."""
    complaint = _password_complaint(password)
    if complaint:
        raise ValueError(complaint)
    cfg = load_config()
    salt = secrets.token_bytes(16)
    cfg["salt"] = salt.hex()
    cfg["kdf"] = CURRENT_KDF
    cfg["password_hash"] = _hash_password(password, salt, CURRENT_KDF)
    cfg["session_secret"] = secrets.token_hex(32)
    save_config(cfg)


def verify_password(password: str, cfg: dict[str, Any] | None = None) -> bool:
    cfg = cfg if cfg is not None else load_config()
    if not cfg.get("password_hash") or not cfg.get("salt"):
        return False
    try:
        salt = bytes.fromhex(cfg["salt"])
        candidate = _hash_password(password, salt, cfg.get("kdf") or KDF_PBKDF2)
    except ValueError:
        # Corrupt salt, or a kdf name this build doesn't know (a config
        # written by a NEWER RELION-US). Refusing the login is the only safe
        # answer -- never fall through to "no password set".
        return False
    return hmac.compare_digest(candidate, cfg["password_hash"])


def needs_rehash(cfg: dict[str, Any] | None = None) -> bool:
    """Whether the stored hash was made by an older KDF than the current one."""
    cfg = cfg if cfg is not None else load_config()
    return bool(cfg.get("password_hash")) and cfg.get("kdf") != CURRENT_KDF


def upgrade_hash_if_needed(password: str, cfg: dict[str, Any] | None = None) -> bool:
    """Re-hash an old PBKDF2 password under scrypt, in place.

    Called only from the login path, with a password that has JUST verified
    -- which is the one moment the plaintext is available to re-hash with,
    and so the only way to migrate without making everyone reset. Returns
    whether anything was written. Deliberately does not rotate the session
    secret the way set_password does: the password itself hasn't changed, so
    logging every device out would be a confusing side effect of an upgrade
    nobody asked for.

    Best-effort: a read-only config directory means the next login just tries
    again, which is strictly better than failing a login that was correct."""
    cfg = cfg if cfg is not None else load_config()
    if not needs_rehash(cfg):
        return False
    fresh = load_config()          # re-read: this runs after a slow KDF
    if not needs_rehash(fresh) or not verify_password(password, fresh):
        return False               # changed underneath us; leave it alone
    salt = secrets.token_bytes(16)
    fresh["salt"] = salt.hex()
    fresh["kdf"] = CURRENT_KDF
    fresh["password_hash"] = _hash_password(password, salt, CURRENT_KDF)
    try:
        save_config(fresh)
    except OSError:
        return False
    return True


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


# --- Online guessing: lockout ----------------------------------------------
# See the LOGIN_* constants for why this exists alongside the KDF cost.

_failed_logins: dict[str, list[float]] = {}


def _prune(times: list[float], now: float) -> list[float]:
    return [t for t in times if now - t < LOGIN_FAILURE_WINDOW]


def login_attempt_allowed(key: str, now: float | None = None) -> tuple[bool, int]:
    """(allowed, seconds_to_wait) for the next login attempt from `key`.

    `key` is the client's address; the caller decides what that means. A
    second, fixed "global" key is checked alongside it by
    record_failed_login, so someone rotating source addresses still trips a
    limit -- per-address alone would be bypassed by exactly that.

    Read-only: call record_failed_login on an actual failure.
    """
    now = time.time() if now is None else now
    for bucket in (key, _GLOBAL_KEY):
        times = _prune(_failed_logins.get(bucket, []), now)
        _failed_logins[bucket] = times
        if len(times) >= _limit_for(bucket):
            wait = int(LOGIN_LOCKOUT_SECONDS - (now - max(times))) + 1
            if wait > 0:
                return False, wait
            # Lockout elapsed -- forget the failures rather than making the
            # next single mistake re-lock instantly.
            _failed_logins[bucket] = []
    return True, 0


_GLOBAL_KEY = "*"


def _limit_for(bucket: str) -> int:
    # The global bucket is deliberately looser than a single address's: it is
    # a backstop against address rotation, and setting it equal would let one
    # clumsy user lock out the whole instance.
    return LOGIN_MAX_FAILURES * 4 if bucket == _GLOBAL_KEY else LOGIN_MAX_FAILURES


def record_failed_login(key: str, now: float | None = None) -> None:
    now = time.time() if now is None else now
    for bucket in (key, _GLOBAL_KEY):
        _failed_logins[bucket] = _prune(_failed_logins.get(bucket, []), now) + [now]
    # Bound memory: without this, one address per request grows this dict
    # forever. Anything already outside the window is dead weight.
    if len(_failed_logins) > 4096:
        for k in [k for k, v in _failed_logins.items() if not v and k != _GLOBAL_KEY]:
            _failed_logins.pop(k, None)


def clear_failed_logins(key: str) -> None:
    """A correct password clears that address's failures -- but NOT the global
    bucket, which would otherwise let an attacker who knows any one valid
    login reset the backstop for everyone."""
    _failed_logins.pop(key, None)


def reset_login_throttle() -> None:
    """Test hook -- module state is process-global, so a test that fills the
    buckets would otherwise leak into whatever runs next."""
    _failed_logins.clear()


# --- TLS --------------------------------------------------------------------

def tls_paths(cfg: dict[str, Any] | None = None) -> tuple[str | None, str | None]:
    """The stored (cert, key) paths, or (None, None). Existence is checked by
    the caller at start time, not here -- a path recorded months ago can be
    gone, and the CLI wants to say so rather than silently serving plain
    HTTP."""
    cfg = cfg if cfg is not None else load_config()
    return cfg.get("tls_cert"), cfg.get("tls_key")


def set_tls_paths(cert: str | None, key: str | None) -> None:
    cfg = load_config()
    cfg["tls_cert"] = str(cert) if cert else None
    cfg["tls_key"] = str(key) if key else None
    save_config(cfg)


def default_cert_paths() -> tuple[Path, Path]:
    root = project_manager.config_root()
    return root / "tls_cert.pem", root / "tls_key.pem"


def generate_self_signed_cert(hostname: str, days: int = 825) -> tuple[Path, Path, str]:
    """Generate a self-signed cert+key with `openssl`, returning
    (cert, key, sha256_fingerprint).

    Shells out to openssl rather than adding `cryptography` to
    requirements.txt: openssl is already present anywhere RELION is (RELION
    links it), and this app's whole install story is "no more dependencies
    than necessary". Raises RuntimeError with openssl's own message if it
    isn't there or fails.

    825 days is the CA/Browser Forum's maximum for a publicly-trusted leaf;
    matching it here means a browser that enforces that ceiling on ALL certs
    (Safari does, for anything issued after 2019) won't reject this one for
    lasting too long.

    The SAN covers `hostname` plus localhost/127.0.0.1/::1, because the
    normal way to reach this app is an SSH tunnel to localhost even when the
    certificate names the real host. A cert without a matching SAN entry is
    rejected outright by every current browser -- CN alone has not been
    accepted for years.
    """
    cert_path, key_path = default_cert_paths()
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    san = f"DNS:{hostname},DNS:localhost,IP:127.0.0.1,IP:::1"
    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:4096", "-nodes",
        "-keyout", str(key_path), "-out", str(cert_path),
        "-days", str(days), "-subj", f"/CN={hostname}",
        "-addext", f"subjectAltName={san}",
        "-addext", "basicConstraints=critical,CA:FALSE",
        "-addext", "keyUsage=critical,digitalSignature,keyEncipherment",
        "-addext", "extendedKeyUsage=serverAuth",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        raise RuntimeError(
            "openssl is not on PATH, so a certificate can't be generated here. "
            "Install it, or point --tls-cert/--tls-key at a certificate you "
            "already have."
        ) from None
    except subprocess.TimeoutExpired:
        raise RuntimeError("openssl did not finish within 120s.") from None
    if proc.returncode != 0:
        raise RuntimeError(f"openssl failed: {(proc.stderr or '').strip()}")
    # The private key is the whole secret here -- openssl writes it at the
    # umask, which on most systems is world-readable.
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    return cert_path, key_path, cert_fingerprint(cert_path)


def cert_fingerprint(cert_path: Path) -> str:
    """SHA-256 fingerprint of a certificate, as openssl prints it.

    This is what makes a self-signed cert genuinely checkable: compare what
    the browser shows against this and a man-in-the-middle is ruled out,
    which is the one guarantee a CA would otherwise be providing."""
    try:
        proc = subprocess.run(
            ["openssl", "x509", "-in", str(cert_path), "-noout",
             "-fingerprint", "-sha256"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "(could not read fingerprint)"
    if proc.returncode != 0:
        return "(could not read fingerprint)"
    return (proc.stdout or "").strip().split("=", 1)[-1].strip()


# --- Is this certificate one HSTS is safe to send with? ---------------------
#
# HSTS is the one setting in this app that a user cannot undo from the machine
# they are on. `Strict-Transport-Security: max-age=31536000` is cached BY THE
# BROWSER for a year; turning the flag back off, restarting, even deleting the
# certificate changes nothing, because the server was never holding the state.
#
# With a self-signed certificate that is not merely inconvenient, it is a
# lockout with no way back: HSTS deliberately removes the click-through on
# certificate warnings (RFC 6797 §12.1 -- "there is no such recourse"), so the
# browser will refuse plain HTTP to that host AND refuse to let anyone accept
# the untrusted certificate. Both doors, for a year, per browser.
#
# The server-side escape (serving `max-age=0` to clear the pin) needs a
# handshake the browser will complete -- which under an active pin with an
# untrusted certificate it won't. So the remedy exists exactly where it isn't
# needed and is unavailable exactly where it is.
#
# Hence: classify the certificate before letting --hsts arm.

TRUST_TRUSTED = "trusted"
TRUST_SELF_SIGNED = "self_signed"
TRUST_UNVERIFIED = "unverified"


def certificate_trust(cert_path: Path) -> tuple[str, str]:
    """Classify a certificate as (status, human-readable detail).

    Three outcomes, deliberately not two:

    * TRUST_SELF_SIGNED -- issuer equals subject. This is what --make-cert
      produces, and the case that bricks a hostname under HSTS. Refused.
    * TRUST_UNVERIFIED -- chains to something, but this machine's trust store
      can't verify it (a private/institutional CA whose root isn't installed
      here, or an expired certificate). Warned about, not refused: a private CA
      IS usually installed in the browsers that matter even when it isn't in
      this machine's OpenSSL store, so refusing outright would be a false
      positive on a deliberate, well-understood setup.
    * TRUST_TRUSTED -- verifies against the system store. HSTS is fine.

    Biased toward the safe answer only where the answer is unambiguous
    (issuer == subject is not a heuristic), because a false refusal costs one
    override flag while a false pass costs a year of that hostname.
    """
    cert_path = Path(cert_path)
    try:
        meta = subprocess.run(
            ["openssl", "x509", "-in", str(cert_path), "-noout", "-issuer", "-subject"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return TRUST_UNVERIFIED, f"could not read the certificate ({exc})"
    if meta.returncode != 0:
        return TRUST_UNVERIFIED, (meta.stderr or "could not read the certificate").strip()

    fields = {}
    for line in (meta.stdout or "").splitlines():
        key, _, value = line.partition("=")
        fields[key.strip()] = value.strip()
    issuer, subject = fields.get("issuer"), fields.get("subject")
    if issuer and subject and issuer == subject:
        return TRUST_SELF_SIGNED, f"self-signed (issuer and subject are both {subject})"

    # -untrusted <the file itself> so a fullchain.pem's own bundled
    # intermediates are used; without it a perfectly good Let's Encrypt
    # certificate fails to verify for lack of its intermediate.
    try:
        verify = subprocess.run(
            ["openssl", "verify", "-untrusted", str(cert_path), str(cert_path)],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return TRUST_UNVERIFIED, f"could not verify the certificate chain ({exc})"
    if verify.returncode == 0:
        return TRUST_TRUSTED, "verifies against this machine's trust store"
    # openssl prints the useful reason ("error 20 at 0 depth lookup: unable to
    # get local issuer certificate") ABOVE its final "error <path>: verification
    # failed" line, so taking the last line yields only the path back. Prefer
    # the depth line, which is the part that says what is actually wrong.
    lines = [ln.strip() for ln in (verify.stderr or verify.stdout or "").splitlines() if ln.strip()]
    reason = next((ln.split("depth lookup:", 1)[1].strip()
                   for ln in lines if "depth lookup:" in ln), None)
    if reason is None:
        reason = next((ln for ln in lines if not ln.startswith("error ")), None)
    return TRUST_UNVERIFIED, f"could not be verified here ({reason or 'chain incomplete'})"


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
            complaint = _password_complaint(pw1)
            if complaint:
                print(complaint)
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
    try:
        set_password(pw)
    except ValueError as exc:
        # _prompt_new_password already applied the same rule, so reaching
        # here means the two disagree -- report rather than crash.
        print(str(exc))
        return 1
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
    if has_pw:
        kdf = cfg.get("kdf") or KDF_PBKDF2
        note = "" if kdf == CURRENT_KDF else "  (upgrades to scrypt on next login)"
        print(f"Hashed with:  {kdf}{note}")
    print(f"Protection:   {'ON' if is_enabled(cfg) else 'OFF'}")
    cert, key = tls_paths(cfg)
    if cert and key:
        missing = [p for p in (cert, key) if not Path(p).exists()]
        if missing:
            print(f"TLS:          configured, but MISSING: {', '.join(missing)}")
        else:
            print(f"TLS:          {cert}")
            print(f"  key:        {key}")
            print(f"  SHA-256:    {cert_fingerprint(Path(cert))}")
    else:
        print("TLS:          not set up -- run `Run-RelionUS --make-cert` "
              "(traffic is plain text without it)")
    return 0


def cli_make_cert(hostname: str | None = None) -> int:
    host = hostname or socket.getfqdn() or "localhost"
    print(f"Generating a self-signed certificate for {host} ...")
    try:
        cert, key, fingerprint = generate_self_signed_cert(host)
    except RuntimeError as exc:
        print(str(exc))
        return 1
    set_tls_paths(str(cert), str(key))
    print(f"  certificate: {cert}")
    print(f"  private key: {key}  (0600)")
    print(f"  SHA-256:     {fingerprint}")
    print()
    print("Start with HTTPS:  ./Run-RelionUS --tls")
    print()
    print("Your browser will warn once that this certificate isn't from a")
    print("recognized authority -- that is expected, and it does NOT mean the")
    print("connection is unencrypted. Check the fingerprint it shows matches")
    print("the SHA-256 above, then accept it. To avoid the warning entirely,")
    print("use a certificate from your institution or Let's Encrypt with")
    print("--tls-cert/--tls-key instead.")
    return 0


def cli_hsts_check(cert: str, forced: bool = False) -> int:
    """Gate for Run-RelionUS's --hsts. Exit codes, not text, are the contract:

      0  safe -- certificate verifies against the system trust store
      2  REFUSE -- self-signed; HSTS would brick this hostname per browser
      1  warn only -- can't be verified here, but isn't self-signed

    Only exit 2 stops the launcher (and only without --hsts-force), because
    only issuer==subject is a certain answer -- see certificate_trust."""
    status, detail = certificate_trust(Path(cert))
    if status == TRUST_TRUSTED:
        return 0
    if status == TRUST_SELF_SIGNED and forced:
        # --hsts-force was passed, so the launcher is going ahead regardless.
        # Printing the full refusal here and then starting anyway would be
        # a contradiction; say what is actually about to happen instead.
        print(f"--hsts-force: sending HSTS with a {detail} certificate.",
              file=sys.stderr)
        print("Browsers will refuse plain HTTP to this host for a year, and "
              "will no longer", file=sys.stderr)
        print("offer to accept this certificate. Clearing that needs manual "
              "steps in every", file=sys.stderr)
        print('browser -- see "Recovering from HSTS" in the README.',
              file=sys.stderr)
        return 0
    if status == TRUST_SELF_SIGNED:
        print(f"Refusing --hsts: this certificate is {detail}.", file=sys.stderr)
        print(file=sys.stderr)
        print("HSTS tells browsers to refuse plain HTTP to this host for a "
              "year, and it is", file=sys.stderr)
        print("cached by the BROWSER -- turning the flag off again, restarting, "
              "or deleting the", file=sys.stderr)
        print("certificate will not undo it. With a self-signed certificate it "
              "also removes the", file=sys.stderr)
        print("click-through on the certificate warning, so you would lose "
              "both http:// and", file=sys.stderr)
        print("https:// on this hostname until you clear HSTS state in every "
              "browser by hand.", file=sys.stderr)
        print(file=sys.stderr)
        print("Use --hsts only with a CA-issued certificate (--tls-cert/"
              "--tls-key). If you", file=sys.stderr)
        print("genuinely mean it anyway, add --hsts-force -- and read "
              '"Recovering from HSTS"', file=sys.stderr)
        print("in the README first.", file=sys.stderr)
        return 2
    print(f"Warning: --hsts is on, but this certificate {detail}.", file=sys.stderr)
    print("If browsers don't trust it either, HSTS will make this hostname "
          "unreachable", file=sys.stderr)
    print('until HSTS state is cleared by hand -- see "Recovering from HSTS" '
          "in the README.", file=sys.stderr)
    return 1


def cli_tls_paths() -> int:
    """Silent except for the paths -- Run-RelionUS reads these to build its
    uvicorn command. Exit 1 (printing nothing) when TLS isn't configured, so
    the caller can branch on the exit code alone."""
    cert, key = tls_paths()
    if not cert or not key:
        return 1
    if not Path(cert).exists() or not Path(key).exists():
        return 1
    print(cert)
    print(key)
    return 0


def cli_is_enabled() -> int:
    """Silent -- exit code only. Run-RelionUS's startup prompt uses this to
    decide whether to ask about password protection this run: skip asking
    if it's already enabled (a login prompt already gates every session in
    that case), ask again every time otherwise -- unlike the old one-time
    prompt, declining doesn't opt you out of being asked on the next run."""
    return 0 if is_enabled() else 1


def main(argv: list[str]) -> int:
    commands = {
        "status": cli_status,
        "set-password": cli_set_password,
        "enable": cli_enable,
        "disable": cli_disable,
        "is-enabled": cli_is_enabled,
        "tls-paths": cli_tls_paths,
    }
    # make-cert is the one verb taking an argument (the hostname to put in
    # the certificate), so it's matched before the no-argument table.
    if argv and argv[0] == "make-cert":
        if len(argv) > 2:
            print("Usage: make-cert [hostname]")
            return 2
        return cli_make_cert(argv[1] if len(argv) == 2 else None)
    if argv and argv[0] == "hsts-check":
        if len(argv) not in (2, 3) or (len(argv) == 3 and argv[2] != "force"):
            print("Usage: hsts-check <certificate> [force]")
            return 2
        return cli_hsts_check(argv[1], forced=len(argv) == 3)
    if len(argv) != 1 or argv[0] not in commands:
        print(f"Usage: {Path(sys.argv[0]).name} "
              "{status|set-password|enable|disable|is-enabled|tls-paths|"
              "make-cert [hostname]|hsts-check <cert>}")
        return 2
    return commands[argv[0]]()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
