"""SSH-key actions use temporary files and real public-key validation."""
import subprocess

import pytest


@pytest.fixture
def key_store(arui_env, monkeypatch):
    from backend.app import authkeys

    folder = arui_env / "isolated-ssh"
    folder.mkdir()
    path = folder / "authorized_keys"
    monkeypatch.setattr(authkeys, "_SSH_DIR", str(folder))
    monkeypatch.setattr(authkeys, "_AUTH", str(path))
    keys = []
    for name in ("first", "second"):
        private = folder / name
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "",
                        "-f", str(private), "-C", name], check=True,
                       capture_output=True, timeout=10)
        keys.append(private.with_suffix(".pub").read_text().strip())
    return authkeys, path, keys


@pytest.mark.parametrize("malformed", ["ssh-ed25519 nonsense", "ssh-rsa AAAA", 4])
def test_add_rejects_unparseable_key_without_touching_file(key_store, malformed):
    authkeys, path, keys = key_store
    path.write_text(keys[0] + "\n")
    original = path.read_bytes()
    assert authkeys.add_key(malformed)["ok"] is False
    assert path.read_bytes() == original


def test_same_key_with_changed_comment_is_duplicate(key_store):
    authkeys, path, keys = key_store
    assert authkeys.add_key(keys[0])["ok"] is True
    other_comment = " ".join(keys[0].split()[:2]) + " different-comment"
    assert authkeys.add_key(other_comment)["ok"] is False
    assert path.read_text().splitlines() == [keys[0]]


def test_invalid_existing_lines_do_not_defeat_last_key_guard(key_store):
    authkeys, path, keys = key_store
    original = keys[0] + "\nssh-ed25519 malformed\nlegacy invalid line\n"
    path.write_text(original)
    result = authkeys.delete_key(authkeys._fingerprint(keys[0]))
    assert result["ok"] is False
    assert "last valid key" in result["error"]
    assert path.read_text() == original


def test_duplicate_lines_cannot_all_be_removed_as_last_key(key_store):
    authkeys, path, keys = key_store
    original = (keys[0] + "\n") * 2
    path.write_text(original)
    assert authkeys.delete_key(authkeys._fingerprint(keys[0]))["ok"] is False
    assert path.read_text() == original


def test_delete_one_key_preserves_other_valid_and_malformed_lines(key_store):
    authkeys, path, keys = key_store
    path.write_text("\n".join([keys[0], "legacy invalid line", keys[1]]) + "\n")
    assert authkeys.delete_key(authkeys._fingerprint(keys[0]))["ok"] is True
    assert path.read_text().splitlines() == ["legacy invalid line", keys[1]]
    assert path.stat().st_mode & 0o777 == 0o600


def test_blank_fingerprint_cannot_delete_malformed_rows(key_store):
    authkeys, path, keys = key_store
    original = "\n".join([keys[0], "ssh-ed25519 malformed", "legacy invalid line"]) + "\n"
    path.write_text(original)
    assert authkeys.delete_key("")["ok"] is False
    assert path.read_text() == original
