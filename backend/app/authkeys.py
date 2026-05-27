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


_SSH_INFO_CACHE: dict = {}


def detect_ssh_info() -> dict:
    """Best-effort detection of how to SSH into this node:
      user   - whoami
      host   - the node's public IP (from ipify, since the host the user is
               reaching the dashboard at is likely behind a cloudflared
               tunnel that doesn't accept SSH)
      port   - the lowest non-default port sshd is listening on (RunPod and
               vast.ai both expose sshd on a high port; if only :22 is open
               we report :22)
      command - the assembled `ssh user@host -p port` string

    The result is cached for the process — public-IP lookups aren't free
    and the answer doesn't change. The user can still override any of
    these fields from Settings.
    """
    global _SSH_INFO_CACHE
    if _SSH_INFO_CACHE.get("command"):
        return dict(_SSH_INFO_CACHE)

    user = "root"
    try:
        u = subprocess.run(["whoami"], capture_output=True, text=True,
                           timeout=3)
        if u.returncode == 0 and u.stdout.strip():
            user = u.stdout.strip()
    except Exception:
        pass

    # public IP
    host = ""
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip",
                "https://icanhazip.com"):
        try:
            import urllib.request
            with urllib.request.urlopen(url, timeout=4) as resp:
                txt = resp.read().decode("utf-8", errors="ignore").strip()
            # very rough IPv4 sanity check
            parts = txt.split(".")
            if (len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255
                                        for p in parts)):
                host = txt
                break
        except Exception:
            continue

    # ssh port — prefer a non-22 listening port (typical of RunPod/vast)
    port = "22"
    try:
        ss = subprocess.run(["ss", "-lntp"], capture_output=True, text=True,
                            timeout=5)
        if ss.returncode == 0:
            ports: list[int] = []
            for ln in ss.stdout.splitlines():
                if "sshd" not in ln:
                    continue
                # local-address is in column 4 (Local Address:Port)
                cols = ln.split()
                addr = next((c for c in cols if ":" in c
                             and not c.startswith("users:")), "")
                if not addr:
                    continue
                p = addr.rsplit(":", 1)[-1]
                if p.isdigit():
                    ports.append(int(p))
            non22 = [p for p in ports if p != 22]
            if non22:
                port = str(min(non22))
            elif 22 in ports:
                port = "22"
    except Exception:
        pass

    cmd = f"ssh {user}@{host} -p {port}" if host else \
          f"ssh {user}@<node-ip> -p {port}"

    out = {"user": user, "host": host, "port": port, "command": cmd}
    _SSH_INFO_CACHE = out
    return dict(out)


def local_pubkey() -> dict:
    """Return this node's SSH public key + a one-liner to install it on
    another machine. If no keypair exists, generate one (ed25519). The user
    pastes this key into another node's authorized_keys so this autoresearcher
    can SSH into it (e.g. to attach a remote GPU server)."""
    priv = os.path.join(_SSH_DIR, "id_ed25519")
    pub = priv + ".pub"
    try:
        os.makedirs(_SSH_DIR, exist_ok=True)
        try:
            os.chmod(_SSH_DIR, 0o700)
        except OSError:
            pass
        if not os.path.exists(pub):
            # generate a fresh ed25519 keypair non-interactively
            subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "",
                            "-f", priv,
                            "-C", "autoresearcherUI@" + _hostname()],
                           capture_output=True, timeout=20)
        if not os.path.exists(pub):
            return {"ok": False, "error": "could not read or create pub key"}
        with open(pub) as f:
            key = f.read().strip()
        return {"ok": True, "pubkey": key, "fingerprint": _fingerprint(key),
                "install_one_liner": (
                    'echo ' + _shell_quote(key) +
                    ' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys')}
    except Exception as e:                             # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _hostname() -> str:
    try:
        return subprocess.run(["hostname"], capture_output=True, text=True,
                              timeout=4).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


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
