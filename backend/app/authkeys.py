"""Manage /root/.ssh/authorized_keys for the System menu's authorized_keys tab.

Writes are safe-ish (per the design review): a timestamped backup first, an
atomic temp+rename, 0700/0600 modes, key-format validation, delete-by-
fingerprint, and a refusal to remove the last key (lockout guard).
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
import tempfile

_SSH_DIR = "/root/.ssh"
_AUTH = os.path.join(_SSH_DIR, "authorized_keys")
_KEY_TYPES = (
    "ssh-ed25519", "ssh-rsa", "ssh-dss",
    "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com",
)


def _read_lines() -> list[str]:
    if not os.path.exists(_AUTH):
        return []
    with open(_AUTH, errors="ignore") as f:
        return [ln.rstrip("\r\n") for ln in f
                if ln.strip() and not ln.lstrip().startswith("#")]


def _fingerprint(line: str) -> str:
    """ssh-keygen fingerprint of a key line, or '' if it can't be parsed."""
    tmp = ""
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".pub",
                                         delete=False) as t:
            t.write(line.strip() + "\n")
            tmp = t.name
        out = subprocess.run(["ssh-keygen", "-l", "-f", tmp],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip()
    except Exception:                                # noqa: BLE001
        return ""
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def list_keys() -> list[dict]:
    keys = []
    for ln in _read_lines():
        parts = ln.split()
        ktype = next((p for p in parts if p in _KEY_TYPES), "")
        comment = (parts[-1] if len(parts) >= 3
                   and parts[-1] not in _KEY_TYPES else "")
        keys.append({"line": ln, "type": ktype, "comment": comment,
                     "fingerprint": _fingerprint(ln)})
    return keys


def _valid(line: str) -> bool:
    line = line.strip()
    if not line or "\n" in line or "\r" in line:
        return False
    parts = line.split()
    if len(parts) < 2:
        return False
    return any(p in _KEY_TYPES for p in parts[:2])


def _write(lines: list[str]) -> None:
    os.makedirs(_SSH_DIR, exist_ok=True)
    try:
        os.chmod(_SSH_DIR, 0o700)
    except OSError:
        pass
    if os.path.exists(_AUTH):                         # timestamped backup
        ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            with open(_AUTH) as src, open(f"{_AUTH}.bak.{ts}", "w") as dst:
                dst.write(src.read())
        except OSError:
            pass
    fd, tmp = tempfile.mkstemp(dir=_SSH_DIR)           # atomic temp + rename
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    os.chmod(tmp, 0o600)
    os.replace(tmp, _AUTH)


def add_key(line: str) -> dict:
    line = (line or "").strip()
    if not _valid(line):
        return {"ok": False,
                "error": "not a valid SSH public key (need e.g. "
                         "'ssh-ed25519 AAAA... comment')"}
    lines = _read_lines()
    fp_new = _fingerprint(line)
    for ln in lines:
        if ln.strip() == line or (fp_new and _fingerprint(ln) == fp_new):
            return {"ok": False, "error": "that key is already authorized"}
    lines.append(line)
    _write(lines)
    return {"ok": True}


def delete_key(fingerprint: str) -> dict:
    lines = _read_lines()
    if len(lines) <= 1:
        return {"ok": False,
                "error": "refusing to remove the last key — that would lock "
                         "you out of the node"}
    keep = [ln for ln in lines if _fingerprint(ln) != fingerprint]
    if len(keep) == len(lines):
        return {"ok": False, "error": "no key with that fingerprint found"}
    _write(keep)
    return {"ok": True}
