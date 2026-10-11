"""Confine a command the way DSH's local sandbox does in `workspace-write` mode.

Mirrors https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/sandbox/sandbox-local/src/profiles.ts:
reads and network are unrestricted, writes are limited to the workspace and the temp areas. Like DSH it fails
closed: without a usable sandbox nothing is launched.
"""

import os
import shutil
import subprocess
import sys
import tempfile


def confine(argv: list[str], workspace: str) -> list[str]:
    root = os.path.realpath(workspace)
    if sys.platform == "darwin":
        user_temp = subprocess.run(["getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True).stdout.strip()
        roots = dict.fromkeys(os.path.realpath(path) for path in (root, "/tmp", tempfile.gettempdir(), user_temp) if path)
        writable = " ".join(f"(subpath {_sbpl(path)})" for path in roots)
        profile = f'(version 1) (allow default) (deny file-write*) (allow file-write* (literal "/dev/null")) (allow file-write* {writable})'
        return ["sandbox-exec", "-p", profile, *argv]
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        return [
            "bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--unshare-pid", "--proc", "/proc", "--die-with-parent",
            "--tmpfs", "/tmp", "--bind", root, root, "--", *argv,
        ]
    raise RuntimeError(f"no sandbox available on {sys.platform}: macOS needs sandbox-exec, Linux needs bwrap")


def _sbpl(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'
