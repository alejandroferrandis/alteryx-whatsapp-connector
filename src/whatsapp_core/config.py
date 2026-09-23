"""Tool settings: parsing, defaults and validation.

Alteryx hands a plugin its configuration as a flat ``dict[str, str]`` built from
the XML the configuration panel saved. Strings arrive for everything - there are
no booleans or integers - and any key the user never touched is simply absent.

Rather than scatter ``tool_config.get("Foo", "false") == "true"`` across the
plugins, every setting is declared once here, with its default, its type and its
validation. Two benefits follow:

* the plugins read typed attributes and stay readable,
* the settings can be exercised from tests and the CLI with no Designer present.

Adding a setting means touching this file and the matching HTML panel, nothing
else.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from .errors import ConfigError
from .profiles import DEFAULT_PROFILE, validate_profile_name

# --------------------------------------------------------------------------
# primitive coercion
# --------------------------------------------------------------------------

_TRUE = {"true", "1", "yes", "on", "checked"}
_FALSE = {"false", "0", "no", "off", "unchecked", ""}


def unwrap(value: Any) -> Any:
    """Reduce one entry of ``tool_config`` to a plain scalar.

    Alteryx builds ``tool_config`` with ``xmltodict.parse``, so the shape of a
    setting depends on how the configuration panel wrote it:

    ===========================  ==================================
    ``<Profile>default</Profile>``   ``"default"``
    ``<Profile value="default"/>``   ``{"@value": "default"}``
    ``<Profile/>``                   ``None``
    ===========================  ==================================

    The HTML GUI SDK writes the **attribute** form, so most settings arrive as
    single-key dictionaries. Without this, ``str()`` of that dictionary becomes
    the setting's value - a profile named ``{'@value': 'default'}`` or, worse, a
    data directory containing characters Windows rejects, which surfaces as a
    baffling ``[Errno 22] Invalid argument`` far from the cause.

    Nested text is also handled (``{"#text": "..."}``), and a dictionary that is
    neither is returned untouched so the caller's own validation reports it.
    """
    if isinstance(value, dict):
        for key in ("@value", "#text", "value"):
            if key in value:
                return value[key]
    return value


def as_bool(value: Any, default: bool = False) -> bool:
    """Coerce an Alteryx config value to a bool.

    The HTML GUI SDK writes ``True``/``False`` for check boxes, but hand-edited
    workflows and older versions can contain any of the usual spellings, so all
    of them are accepted. Anything unrecognised falls back to ``default`` rather
    than raising: a malformed checkbox should not stop a workflow.
    """
    value = unwrap(value)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return default


def as_int(value: Any, default: int, *, minimum: int | None = None,
           maximum: int | None = None, name: str = "value") -> int:
    """Coerce to int and clamp into range.

    Clamping rather than raising suits numeric spinners: the GUI
    already constrains them, so an out-of-range value means a hand-edited
    workflow, and silently honouring the nearest legal value is friendlier than
    failing the run.
    """
    value = unwrap(value)
    if value is None or str(value).strip() == "":
        return default
    try:
        number = int(float(str(value).strip()))
    except (TypeError, ValueError):
        raise ConfigError(f"{name} must be a whole number, got '{value}'.") from None
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def as_text(value: Any, default: str = "") -> str:
    value = unwrap(value)
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def as_datetime(value: Any, name: str, *, end_of_day: bool = False) -> datetime | None:
    """Parse a date or date-time from the GUI's DateTimeField, as UTC.

    Two conversions happen here, and both exist because of what a person means
    when they pick a date out of a calendar.

    **A date-only bound is a whole day.** ``2026-09-23`` as a *start* is that
    day's first instant; as an *end* it is that day's last instant. Treating an
    end date as midnight excludes the entire day the user just selected - so
    "from today to today" returns nothing, which is exactly the bug this
    replaced. A bound that already carries a time is used as given.

    **Dates are local, timestamps are UTC.** Someone choosing "23 September"
    means their own 23rd. The archive stores UTC, so the local bound is
    converted. For a user two hours ahead of UTC this moves the boundary by two
    hours - invisible when it is right, and baffling when it is not, which is
    why the effective UTC window is written to the run log.
    """
    text = as_text(value)
    if not text:
        return None

    for fmt, is_date_only in (
        ("%Y-%m-%d %H:%M:%S", False),
        ("%Y-%m-%dT%H:%M:%S", False),
        ("%Y-%m-%d", True),
    ):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if is_date_only and end_of_day:
            parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
        # A naive datetime is interpreted as local time by astimezone().
        return parsed.astimezone(timezone.utc)

    raise ConfigError(
        f"{name} is not a valid date: '{text}'. Expected YYYY-MM-DD or "
        "YYYY-MM-DD HH:MM:SS."
    )


def split_list(value: Any) -> list[str]:
    """Split a comma/newline/semicolon separated field into clean entries."""
    text = as_text(value)
    if not text:
        return []
    out = []
    for chunk in text.replace("\r", "\n").replace(";", "\n").replace(",", "\n").split("\n"):
        item = chunk.strip()
        if item:
            out.append(item)
    return out


# --------------------------------------------------------------------------
# settings shared by both tools
# --------------------------------------------------------------------------


@dataclass
class ConnectionSettings:
    """Everything needed to open (or create) a WhatsApp session.

    Both tools embed this, which is why the Connection section of the two
    configuration panels is identical - by design, so a user who has set up one
    tool already knows the other.
    """

    profile: str = DEFAULT_PROFILE
    data_dir: str = ""

    #: When true this run links a device instead of doing its normal job.
    link_device: bool = False
    #: Phone number to link, in any human format. Empty means "show a QR code".
    link_phone: str = ""
    #: Discard the existing session before linking. Needed after a logout.
    force_relink: bool = False

    #: Seconds to wait for the WebSocket handshake and login.
    connect_timeout: int = 60
    #: Optional outbound proxy, e.g. ``http://user:pass@host:3128`` or socks5://.
    proxy_url: str = ""
    #: Name shown on the phone under Linked devices.
    device_name: str = "Alteryx"

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "ConnectionSettings":
        settings = cls(
            profile=validate_profile_name(as_text(config.get("Profile"), DEFAULT_PROFILE)),
            data_dir=as_text(config.get("DataDir")),
            link_device=as_bool(config.get("LinkDevice")),
            link_phone=as_text(config.get("LinkPhone")),
            force_relink=as_bool(config.get("ForceRelink")),
            connect_timeout=as_int(
                config.get("ConnectTimeout"), 60, minimum=10, maximum=600,
                name="Connection timeout",
            ),
            proxy_url=as_text(config.get("ProxyUrl")),
            device_name=as_text(config.get("DeviceName"), "Alteryx"),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.data_dir:
            expanded = os.path.expanduser(os.path.expandvars(self.data_dir))
            parent = os.path.dirname(expanded.rstrip("\\/")) or expanded
            if not os.path.isdir(expanded) and not os.path.isdir(parent):
                raise ConfigError(
                    f"The data directory '{self.data_dir}' does not exist and its parent "
                    "folder does not either.\n"
                    "Fix: create the folder first, or clear the field to use the default "
                    "location under %LOCALAPPDATA%\\Alteryx\\WhatsAppConnector."
                )
        if self.proxy_url and "://" not in self.proxy_url:
            raise ConfigError(
                f"The proxy '{self.proxy_url}' is missing a scheme. "
                "Use http://host:port, https://host:port or socks5://host:port."
            )

    @property
    def resolved_data_dir(self) -> str:
        return os.path.expanduser(os.path.expandvars(self.data_dir)) if self.data_dir else ""


# --------------------------------------------------------------------------
# Input tool
# --------------------------------------------------------------------------

#: Sync, then emit from the archive. The normal setting.
MODE_SYNC = "Sync"
#: Do not connect at all; re-read what previous runs already collected.
MODE_ARCHIVE = "ArchiveOnly"


@dataclass
class InputSettings:
    """Configuration of the WhatsApp Input tool."""

    connection: ConnectionSettings = field(default_factory=ConnectionSettings)

    mode: str = MODE_SYNC

    #: Stop collecting after this many seconds of *silence* from WhatsApp. The
    #: backlog arrives in a burst on connect, so a short idle window is enough
    #: to know it has all landed.
    idle_timeout: int = 8
    #: Hard ceiling on one sync, whatever the idle timer says.
    max_sync_seconds: int = 120

    #: Chat ids, phone numbers or chat names. Empty means every chat.
    chats: list[str] = field(default_factory=list)
    include_groups: bool = True
    include_direct: bool = True
    #: Messages this account sent are archived either way; this controls output.
    include_own_messages: bool = False

    #: Date bounds apply only when this is on.
    #:
    #: Opt-in because Alteryx's date widget pre-fills itself with today's date.
    #: Without a switch, a user who never touched those fields still gets a
    #: one-day filter, and every message outside it silently disappears - which
    #: is precisely what happened in testing.
    use_date_range: bool = False
    date_from: datetime | None = None
    date_to: datetime | None = None

    #: Emit only messages this profile has never emitted before. This is what
    #: makes an hourly schedule trivial: each run yields exactly what is new.
    only_new: bool = True
    max_records: int = 0  # 0 = unlimited

    download_media: bool = True
    #: Skip media above this size to keep workflows and disks sane.
    max_media_mb: int = 25
    include_raw_json: bool = False

    #: Also publish the chat directory on the second output anchor.
    emit_chats: bool = True

    # -- how far back a sync is allowed to reach --------------------------
    #
    # WhatsApp queues messages for an offline linked device and delivers the
    # whole backlog the moment it reconnects. A workflow paused over a holiday
    # therefore comes back to a flood, and nothing in the tool stopped it.
    # These two settings put a floor under what a sync will ingest.

    #: Never archive a message older than this many days. 0 lifts the limit.
    ignore_older_than_days: int = 7
    #: On a profile's very first sync, treat that moment as the start of time.
    #: Stops a fresh link hoovering up whatever backlog happens to exist.
    start_from_first_run: bool = True

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "InputSettings":
        mode = as_text(config.get("Mode"), MODE_SYNC)
        if mode not in (MODE_SYNC, MODE_ARCHIVE):
            raise ConfigError(
                f"Unknown Input mode '{mode}'. Expected '{MODE_SYNC}' or '{MODE_ARCHIVE}'."
            )
        use_date_range = as_bool(config.get("UseDateRange"), False)
        settings = cls(
            connection=ConnectionSettings.from_config(config),
            mode=mode,
            idle_timeout=as_int(
                config.get("IdleTimeout"), 8, minimum=1, maximum=600,
                name="Stop after idle (seconds)",
            ),
            max_sync_seconds=as_int(
                config.get("MaxSyncSeconds"), 120, minimum=5, maximum=3600,
                name="Maximum sync time (seconds)",
            ),
            chats=split_list(config.get("Chats")),
            include_groups=as_bool(config.get("IncludeGroups"), True),
            include_direct=as_bool(config.get("IncludeDirect"), True),
            include_own_messages=as_bool(config.get("IncludeOwnMessages"), False),
            use_date_range=use_date_range,
            # Only parsed when the range is on. The panel's date widgets keep
            # whatever they last held even while the box is unticked, so
            # parsing unconditionally let a stored value from an older or
            # hand-edited workflow fail a run that does not use dates at all.
            date_from=as_datetime(config.get("DateFrom"), "Date from")
            if use_date_range else None,
            date_to=as_datetime(config.get("DateTo"), "Date to", end_of_day=True)
            if use_date_range else None,
            only_new=as_bool(config.get("OnlyNew"), True),
            max_records=as_int(
                config.get("MaxRecords"), 0, minimum=0, maximum=10_000_000,
                name="Maximum records",
            ),
            download_media=as_bool(config.get("DownloadMedia"), True),
            max_media_mb=as_int(
                config.get("MaxMediaMb"), 25, minimum=0, maximum=2048,
                name="Maximum attachment size (MB)",
            ),
            include_raw_json=as_bool(config.get("IncludeRawJson"), False),
            emit_chats=as_bool(config.get("EmitChats"), True),
            ignore_older_than_days=as_int(
                config.get("IgnoreOlderThanDays"), 7, minimum=0, maximum=3650,
                name="Ignore messages older than (days)",
            ),
            start_from_first_run=as_bool(config.get("StartFromFirstRun"), True),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        # Discard the bounds entirely when the range is switched off, so
        # nothing downstream has to remember to check the flag.
        if not self.use_date_range:
            self.date_from = None
            self.date_to = None

        if not self.include_groups and not self.include_direct:
            raise ConfigError(
                "Both 'Include group chats' and 'Include direct chats' are switched "
                "off, so the tool can never return a row. Switch at least one on."
            )
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ConfigError(
                f"'Date from' ({self.date_from:%Y-%m-%d}) is after 'Date to' "
                f"({self.date_to:%Y-%m-%d})."
            )
        if self.idle_timeout > self.max_sync_seconds:
            raise ConfigError(
                f"'Stop after idle' ({self.idle_timeout}s) cannot be longer than "
                f"'Maximum sync time' ({self.max_sync_seconds}s)."
            )
        if self.mode == MODE_ARCHIVE and self.connection.link_device:
            raise ConfigError(
                "'Link this device' needs a connection, but the tool is set to "
                f"'{MODE_ARCHIVE}'. Switch the mode to '{MODE_SYNC}' to link."
            )


# --------------------------------------------------------------------------
# Output tool
# --------------------------------------------------------------------------

#: Destination / body taken from a column of the incoming data.
SOURCE_FIELD = "Field"
#: Destination / body typed once in the configuration panel.
SOURCE_FIXED = "Fixed"


@dataclass
class OutputSettings:
    """Configuration of the WhatsApp Output tool."""

    connection: ConnectionSettings = field(default_factory=ConnectionSettings)

    to_source: str = SOURCE_FIELD
    to_field: str = ""
    to_fixed: str = ""

    message_source: str = SOURCE_FIELD
    message_field: str = ""
    message_fixed: str = ""

    #: Optional column holding a path to a file to attach.
    attachment_field: str = ""
    #: Optional column holding a caption for that attachment.
    caption_field: str = ""

    #: Prefixed to bare national numbers. See :func:`jid.normalise_phone`.
    default_country_code: str = ""

    #: Outbound throttle. WhatsApp bans numbers that behave like broadcasters,
    #: so the default is a gentle one.
    messages_per_minute: int = 20
    #: Abort the whole run on the first failure instead of reporting per row.
    fail_on_error: bool = False
    #: Verify the number is on WhatsApp before sending to a direct chat.
    verify_recipients: bool = True
    #: Longest a single send may take before it is treated as failed.
    send_timeout: int = 60

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "OutputSettings":
        settings = cls(
            connection=ConnectionSettings.from_config(config),
            to_source=as_text(config.get("ToSource"), SOURCE_FIELD),
            to_field=as_text(config.get("ToField")),
            to_fixed=as_text(config.get("ToFixed")),
            message_source=as_text(config.get("MessageSource"), SOURCE_FIELD),
            message_field=as_text(config.get("MessageField")),
            message_fixed=as_text(config.get("MessageFixed")),
            attachment_field=as_text(config.get("AttachmentField")),
            caption_field=as_text(config.get("CaptionField")),
            default_country_code=as_text(config.get("DefaultCountryCode")),
            messages_per_minute=as_int(
                config.get("MessagesPerMinute"), 20, minimum=1, maximum=600,
                name="Messages per minute",
            ),
            fail_on_error=as_bool(config.get("FailOnError"), False),
            verify_recipients=as_bool(config.get("VerifyRecipients"), True),
            send_timeout=as_int(
                config.get("SendTimeout"), 60, minimum=5, maximum=600,
                name="Send timeout (seconds)",
            ),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        for label, source, field_name, fixed in (
            ("Send to", self.to_source, self.to_field, self.to_fixed),
            ("Message", self.message_source, self.message_field, self.message_fixed),
        ):
            if source not in (SOURCE_FIELD, SOURCE_FIXED):
                raise ConfigError(
                    f"'{label}' has an unknown source '{source}'. "
                    f"Expected '{SOURCE_FIELD}' or '{SOURCE_FIXED}'."
                )
            # A link-only run has no data to read, so an unset field is fine.
            if self.connection.link_device:
                continue
            if source == SOURCE_FIELD and not field_name:
                raise ConfigError(
                    f"'{label}' is set to take its value from a column, but no column "
                    "is selected. Pick one in the tool's configuration panel."
                )
            if source == SOURCE_FIXED and not fixed:
                raise ConfigError(
                    f"'{label}' is set to a fixed value, but the box is empty."
                )
        if self.default_country_code:
            digits = self.default_country_code.lstrip("+").strip()
            if not digits.isdigit() or not 1 <= len(digits) <= 4:
                raise ConfigError(
                    f"'{self.default_country_code}' is not a valid country code. "
                    "Use 1 to 4 digits, for example 1, 44 or 351."
                )

    @property
    def needed_fields(self) -> list[str]:
        """Incoming columns this configuration reads. Used to validate metadata."""
        wanted = []
        if self.to_source == SOURCE_FIELD and self.to_field:
            wanted.append(self.to_field)
        if self.message_source == SOURCE_FIELD and self.message_field:
            wanted.append(self.message_field)
        if self.attachment_field:
            wanted.append(self.attachment_field)
        if self.caption_field:
            wanted.append(self.caption_field)
        return wanted
