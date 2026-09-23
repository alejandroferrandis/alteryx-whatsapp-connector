"""Exception hierarchy.

Every error the connector raises on purpose derives from :class:`WhatsAppError`
and carries a message written for an Alteryx user, in their vocabulary:
it says what went wrong *and* what to do about it. The plugins catch
:class:`WhatsAppError` and surface ``str(exc)`` directly in the Designer results
pane, so these strings are user-facing copy.
"""

from __future__ import annotations


class WhatsAppError(Exception):
    """Base class for every expected failure in the connector."""


class ConfigError(WhatsAppError):
    """The tool configuration is missing something or is self-contradictory."""


class NotLinkedError(WhatsAppError):
    """The selected profile has no linked WhatsApp device yet."""

    def __init__(self, profile: str) -> None:
        super().__init__(
            # Both tools raise this, so it must not claim to be about reading.
            "This tool is not connected to WhatsApp yet, so it has nothing to "
            "work with.\n"
            "Set it up once, from here:\n"
            "  1. Tick 'Link this device' in the Connection section above.\n"
            "  2. Type the phone number of the WhatsApp account you want to use.\n"
            "  3. Run the workflow. An 8-character code appears in this pane.\n"
            "  4. On that phone open WhatsApp > Settings > Linked devices >\n"
            "     Link a device > Link with phone number instead, and type the code.\n"
            "  5. Untick 'Link this device' and run again.\n"
            f'The connection is then remembered for profile "{profile}", and you '
            "will not be asked again."
        )
        self.profile = profile


class LoggedOutError(WhatsAppError):
    """WhatsApp revoked this linked device; only re-pairing can fix it."""

    def __init__(self, profile: str) -> None:
        super().__init__(
            f'WhatsApp has unlinked profile "{profile}" - the device was removed on '
            "the phone, or the account was logged out everywhere.\n"
            "The stored connection is dead and has been marked for replacement.\n"
            "Fix: tick 'Link this device' in the Connection section, enter the phone "
            "number, and run once. The dead connection is discarded automatically, "
            "so there is nothing to clean up first."
        )
        self.profile = profile


class ProfileLockedError(WhatsAppError):
    """Another process already holds this profile's session."""

    def __init__(self, profile: str, holder: str) -> None:
        super().__init__(
            f'Profile "{profile}" is already in use by {holder}.\n'
            "A WhatsApp session can only be open in one process at a time. Fix: let the "
            "other workflow finish, or give this tool a different profile name so it "
            "uses its own linked device."
        )
        self.profile = profile


class ConnectionTimeout(WhatsAppError):
    """The client never reached a connected state inside the allotted time."""

    def __init__(self, seconds: float) -> None:
        super().__init__(
            f"Could not reach WhatsApp within {seconds:g} seconds.\n"
            "Fix: check this machine's internet access and any corporate proxy or "
            "firewall (WhatsApp Web uses a WebSocket to *.whatsapp.net on port 443). "
            "If you connect through a proxy, set it in the tool's Advanced section."
        )


class ConnectionFailed(WhatsAppError):
    """WhatsApp actively refused or dropped the connection.

    Distinct from :class:`ConnectionTimeout`: the server answered, and what it
    said is worth repeating to the user verbatim.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(
            f"WhatsApp refused the connection: {detail}\n"
            "Fix: if this mentions a ban or a policy violation the account needs "
            "attention on the phone itself. Otherwise retry - transient stream "
            "errors are normal and the next run usually succeeds."
        )
        self.detail = detail


class PairingError(WhatsAppError):
    """Linking a device failed or was not completed in time."""


class SendError(WhatsAppError):
    """A single outbound message could not be delivered.

    Raised per row by the sender and caught by the output plugin, which records
    the failure on the result anchor rather than aborting the whole workflow.
    """


class RecipientNotFound(SendError):
    """The destination could not be resolved to a WhatsApp chat."""


class MissingDependencyError(WhatsAppError):
    """A bundled dependency is absent - almost always a broken installation."""

    def __init__(self, name: str, detail: str = "") -> None:
        super().__init__(
            f"The bundled component '{name}' could not be loaded.{(' ' + detail) if detail else ''}\n"
            "Fix: this usually means the .yxi was installed incompletely or a security "
            "product quarantined a file. Reinstall the connector from the .yxi; if the "
            "problem persists, allow-list the Alteryx tools directory in your antivirus."
        )


class ArchiveVersionError(WhatsAppError):
    """The archive was written by a newer build than the one reading it."""
