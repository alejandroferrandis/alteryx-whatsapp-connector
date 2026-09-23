"""Crash reports.

An Alteryx tool has one channel to the user: a line in the Results pane. That is
the right place for an expected failure, which carries its own fix, but it is
useless for an unexpected one - the traceback that would identify the bug is
thrown away, and the user is left reporting "it says Invalid argument".

So anything unexpected is written to a file next to the profile, and the tool
tells the user where. A support request then becomes "send me this file"
instead of a guessing game.

Reports contain the traceback, the environment, and the tool configuration as
Designer actually delivered it - which is frequently the thing that explains
the crash, because the shape of ``tool_config`` depends on how the panel wrote
each setting.
"""

from __future__ import annotations

import json
import platform
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Mapping

#: Keep this many reports per profile; older ones are deleted.
MAX_REPORTS = 20


def _redact(config: Mapping[str, Any]) -> dict:
    """Copy the configuration, hiding anything that could identify a person.

    The phone number being linked is the only personal datum in a tool
    configuration, and a crash report is something a user will paste into an
    email or an issue tracker.
    """
    safe = {}
    for key, value in config.items():
        # "ToFixed" is very often a phone number typed straight into the
        # panel, so a key-name rule alone under-delivers on the promise the
        # plugins make when they hand a user this file.
        lowered = key.lower()
        if "phone" in lowered or "number" in lowered or "fixed" in lowered:
            text = str(value)
            safe[key] = f"<redacted, {len(text)} chars>" if text else ""
        else:
            safe[key] = value
    return safe


def write_report(
    directory: Path,
    exc: BaseException,
    *,
    tool: str,
    tool_config: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> Path | None:
    """Write a crash report and return its path, or ``None`` if that failed.

    Never raises: this runs on the failure path, and a crash inside the crash
    reporter would replace a useful message with a useless one.
    """
    try:
        target_dir = Path(directory) / "logs"
        target_dir.mkdir(parents=True, exist_ok=True)
        # Milliseconds, not seconds: both plugins failing on one canvas crash
        # within the same second, and the second report overwrote the first.
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        path = target_dir / f"crash-{tool}-{stamp}-{int(time.time() * 1000) % 1000:03d}.log"

        from .version import __version__

        lines = [
            f"WhatsApp connector for Alteryx - crash report",
            f"tool           : {tool}",
            f"version        : {__version__}",
            f"time (UTC)     : {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
            f"python         : {sys.version.split()[0]} ({sys.platform})",
            f"os             : {platform.platform()}",
            f"executable     : {sys.executable}",
            "",
            "exception",
            "---------",
            f"{type(exc).__name__}: {exc}",
            "",
            "traceback",
            "---------",
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        ]

        if tool_config is not None:
            lines += [
                "tool configuration as Designer delivered it",
                "-------------------------------------------",
                json.dumps(_redact(tool_config), indent=2, default=str, sort_keys=True),
                "",
            ]
        if extra:
            lines += [
                "context",
                "-------",
                json.dumps(dict(extra), indent=2, default=str, sort_keys=True),
                "",
            ]

        path.write_text("\n".join(lines), encoding="utf-8")
        _trim(target_dir)
        return path
    except Exception:  # noqa: BLE001 - never let reporting mask the real failure
        return None


def _trim(directory: Path) -> None:
    """Keep only the newest MAX_REPORTS files."""
    try:
        reports = sorted(
            directory.glob("crash-*.log"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        for stale in reports[MAX_REPORTS:]:
            stale.unlink(missing_ok=True)
    except OSError:
        pass
