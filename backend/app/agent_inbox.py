"""Private full messages with a short, verifiable native-CLI input."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import tempfile


def _private_file(path: Path, text: str) -> None:
    if path.is_symlink():
        raise OSError('Private inbox paths must not be symlinks')
    if path.exists():
        if not path.is_file() or path.read_text(encoding='utf-8') != text:
            raise OSError('Private inbox path already contains different data')
        path.chmod(0o600)
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.message-', delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def delivery_text(text: str, session: str, *, single_line: bool = False, max_chars: int = 512) -> str:
    """Keep terminal input short without discarding any message context.

    Native CLIs clip large drafts internally, so tmux cannot inspect their
    complete text to verify Enter/retry ownership. A relative inbox reference
    stays visible even when the full message has hundreds of lines.
    """
    # UTF-8 bytes conservatively bound rendered cells for wide Unicode text.
    if (len(text.encode('utf-8')) <= max_chars and text.count('\n') <= 2 and '\r' not in text
            and (not single_line or '\n' not in text)):
        return text
    result = subprocess.run(['tmux', 'display-message', '-p', '-t', '=' + session + ':',
                             '#{pane_current_path}'], capture_output=True, text=True, timeout=4)
    raw = result.stdout.decode() if isinstance(result.stdout, bytes) else result.stdout
    cwd = Path((raw or '').strip())
    if result.returncode or not cwd.is_absolute() or not cwd.is_dir():
        raise OSError('The agent workspace is unavailable')
    cwd = cwd.resolve(strict=True)
    folder = cwd / '.arui-inbox'
    if folder.is_symlink():
        raise OSError('Private inbox paths must not be symlinks')
    folder.mkdir(mode=0o700, exist_ok=True)
    folder.chmod(0o700)
    # An app-owned nested ignore file protects these messages even in a new
    # workspace where Git is initialized after the first message arrives.
    _private_file(folder / '.gitignore', '*\n')
    name = hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]
    _private_file(folder / name, text)
    return f'Follow .arui-inbox/{name}'
