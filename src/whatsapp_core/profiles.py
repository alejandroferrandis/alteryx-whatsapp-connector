"""Profiles: where a linked device's state lives, and who is allowed to use it.

A *profile* is one linked WhatsApp device. It owns a directory::

    <data root>/profiles/<name>/
        session.sqlite3     whatsmeow's device store (the actual link + keys)
        archive.sqlite3     our message archive and chat directory
        media/              files downloaded from incoming messages
        profile.json        human-readable metadata (who is linked, when)
        profile.lock        advisory lock, see LockedProfile

Profiles are the reason the connector supports more than one WhatsApp account
per machine without any extra configuration: the Input and Output tools simply
name the profile they want, and everything else follows from that name.

The data root is resolved in this order:

1. the ``data_dir`` given in the tool's Advanced section,
2. the ``ALTERYX_WHATSAPP_HOME`` environment variable,
3. ``%LOCALAPPDATA%\\Alteryx\\WhatsAppConnector`` on Windows,
   ``~/.local/share/alteryx-whatsapp`` elsewhere.

LOCALAPPDATA rather than APPDATA matters here: the session database is a
machine-local credential that must never follow a roaming profile onto another
machine, where a second live copy of the same device would get both unlinked.
"""

from __future__ import annotations

import json
import os
import platform
import re
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType

from .errors import ConfigError, ProfileLockedError

#: Profile names become directory names, so keep them boring and portable.
_VALID_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")

DEFAULT_PROFILE = "default"


def validate_profile_name(name: str) -> str:
    """Normalise and sanity-check a profile name."""
    cleaned = (name or "").strip()
    if not cleaned:
        return DEFAULT_PROFILE
    if not _VALID_NAME.match(cleaned):
        raise ConfigError(
            f"'{name}' is not a valid profile name. Use letters, digits, spaces, "
            "dots, dashes or underscores (max 64 characters), starting with a "
            "letter or digit."
        )
    return cleaned


def default_data_root() -> Path:
    """Where profiles live when the user has not overridden it."""
    override = os.environ.get("ALTERYX_WHATSAPP_HOME")
    if override:
        return Path(override).expanduser()
    if platform.system() == "Windows":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        return Path(base) / "Alteryx" / "WhatsAppConnector"
    return Path(os.path.expanduser("~")) / ".local" / "share" / "alteryx-whatsapp"


@dataclass
class Profile:
    """Filesystem layout and metadata for one linked device."""

    name: str
    root: Path

    #: Populated from profile.json on load; see :meth:`metadata`.
    _meta: dict = field(default_factory=dict, repr=False)

    # -- construction -----------------------------------------------------

    @classmethod
    def open(cls, name: str, data_dir: str | os.PathLike[str] | None = None) -> "Profile":
        """Return the profile called ``name``, creating its directory if needed."""
        safe = validate_profile_name(name)
        root_base = Path(data_dir).expanduser() if data_dir else default_data_root()
        root = root_base / "profiles" / safe
        try:
            (root / "media").mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigError(
                f"Could not create the profile directory {root}: {exc}.\n"
                "Fix: choose a different data directory in the tool's Advanced "
                "section, or grant this Windows account write access to that path."
            ) from exc
        profile = cls(name=safe, root=root)
        profile._meta = profile._read_meta()
        return profile

    # -- paths ------------------------------------------------------------

    @property
    def session_db(self) -> Path:
        """whatsmeow's device store. Deleting this unlinks the device locally."""
        return self.root / "session.sqlite3"

    @property
    def archive_db(self) -> Path:
        return self.root / "archive.sqlite3"

    @property
    def media_dir(self) -> Path:
        return self.root / "media"

    @property
    def meta_file(self) -> Path:
        return self.root / "profile.json"

    @property
    def lock_file(self) -> Path:
        return self.root / "profile.lock"

    # -- metadata ---------------------------------------------------------

    def _read_meta(self) -> dict:
        try:
            return json.loads(self.meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A missing or corrupt metadata file is not fatal: it is a cache of
            # things we can re-learn from WhatsApp on the next connect.
            return {}

    def metadata(self) -> dict:
        return dict(self._meta)

    def update_metadata(self, **values: object) -> None:
        """Merge ``values`` into profile.json (written atomically)."""
        self._meta.update({k: v for k, v in values.items() if v is not None})
        self._meta["updated_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        tmp = self.meta_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._meta, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.meta_file)

    @property
    def is_linked(self) -> bool:
        """True when a device session exists on disk.

        This is a cheap local check: it proves a link *was* established, not
        that WhatsApp still honours it. Only a connect can prove the latter,
        which is why :class:`~whatsapp_core.errors.LoggedOutError` exists.
        """
        return self.session_db.exists() and self.session_db.stat().st_size > 0

    @property
    def linked_number(self) -> str | None:
        value = self._meta.get("linked_number")
        return str(value) if value else None

    def describe(self) -> str:
        """One-line human summary used in log messages."""
        if not self.is_linked:
            return f'profile "{self.name}" (not linked)'
        who = self.linked_number or "unknown number"
        return f'profile "{self.name}" (linked as {who})'

    def unlink_local(self) -> None:
        """Delete the local session so the next run can pair afresh.

        The archive survives: message history a customer has already
        collected must survive a re-pair.
        """
        for path in (self.session_db, self.meta_file):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        # whatsmeow may leave SQLite side-car files behind.
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                self.session_db.with_name(self.session_db.name + suffix).unlink()
            except FileNotFoundError:
                pass
        self._meta = {}

    # -- locking ----------------------------------------------------------

    def lock(self, timeout: float = 0.0) -> "LockedProfile":
        return LockedProfile(self, timeout=timeout)


class LockedProfile:
    """Advisory, cross-process lock over one profile.

    whatsmeow keeps its device state in SQLite and assumes a single writer; two
    Alteryx tools opening the same session concurrently corrupts the link and
    gets the device unlinked by WhatsApp. Designer happily runs tools in
    parallel, so this guard is not optional.

    The lock is a file containing the owning process's identity. A lock whose
    owning PID is gone is treated as stale and reclaimed, so a crashed workflow
    never wedges a profile permanently.
    """

    def __init__(self, profile: Profile, timeout: float = 0.0) -> None:
        self.profile = profile
        self.timeout = timeout
        self._fd: int | None = None
        #: True only between a successful acquire and its release.
        #:
        #: Releasing is not the same as "having tried to acquire". Without this
        #: flag, a caller that failed to get the lock still deleted the lock
        #: file on the way out - destroying the claim of the process that
        #: actually held it, and letting a third process open the same device
        #: session. That is precisely the corruption this class exists to stop.
        self._acquired = False

    def _owner(self) -> str:
        try:
            data = json.loads(self.profile.lock_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "another process"
        pid = data.get("pid")
        host = data.get("host", "?")
        return f"process {pid} on {host}"

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if platform.system() == "Windows":
            # No os.kill(pid, 0) semantics on Windows; ask the OS directly.
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            exit_code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            kernel32.CloseHandle(handle)
            return bool(ok) and exit_code.value == STILL_ACTIVE
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, ValueError):
            return False
        except PermissionError:
            return True
        return True

    def _reclaim_if_stale(self) -> bool:
        """Delete the lock file if its owner is no longer running."""
        try:
            data = json.loads(self.profile.lock_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return True
        except (OSError, ValueError):
            data = {}
        pid = int(data.get("pid", 0) or 0)
        same_host = data.get("host") == socket.gethostname()
        # Only reclaim locks left by this machine: a PID from another host tells
        # us nothing, and a shared data directory is a supported (if unusual)
        # deployment.
        if same_host and not self._pid_alive(pid):
            try:
                self.profile.lock_file.unlink()
            except FileNotFoundError:
                pass
            return True
        return False

    def __enter__(self) -> Profile:
        deadline = time.monotonic() + max(self.timeout, 0.0)
        payload = json.dumps(
            {
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        ).encode("utf-8")
        while True:
            try:
                self._fd = os.open(
                    self.profile.lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
                os.write(self._fd, payload)
                self._acquired = True
                return self.profile
            except FileExistsError:
                if self._reclaim_if_stale():
                    continue
                if time.monotonic() >= deadline:
                    raise ProfileLockedError(self.profile.name, self._owner()) from None
                time.sleep(0.25)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Release only what we took. Exiting without having acquired is an
        # ordinary situation - the caller timed out, or failed on something
        # else before reaching the lock - and in that case the lock file on
        # disk belongs to somebody else. Also makes a double release harmless.
        if not self._acquired:
            return
        self._acquired = False

        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None
        try:
            self.profile.lock_file.unlink()
        except FileNotFoundError:
            pass


def list_profiles(data_dir: str | os.PathLike[str] | None = None) -> list[Profile]:
    """Every profile directory found under the data root, linked or not."""
    root_base = Path(data_dir).expanduser() if data_dir else default_data_root()
    container = root_base / "profiles"
    if not container.is_dir():
        return []
    found = []
    for child in sorted(container.iterdir()):
        if child.is_dir():
            profile = Profile(name=child.name, root=child)
            profile._meta = profile._read_meta()
            found.append(profile)
    return found
