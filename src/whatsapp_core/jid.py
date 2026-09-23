"""Turning whatever the user typed into a WhatsApp address (a "JID").

Nobody knows that their family group is ``120363000000000000@g.us``, so this
module accepts whatever a person is likely to have to hand, in order of
preference:

* a full JID              ``15550100@s.whatsapp.net``, ``120363000000000000@g.us``
* a phone number          ``+1 555 0100``, ``0015550100``, ``5550100``
* a chat name             ``Family``, ``Ops team``   (resolved via the archive)

Only the first two are handled here, because they need no I/O. Name lookup
lives in :mod:`whatsapp_core.store`, which owns the chat directory; the output
plugin calls :func:`parse_destination` first and falls back to the store.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from .errors import ConfigError

#: Server part of a one-to-one chat with a phone number.
SERVER_USER: Final = "s.whatsapp.net"
#: Server part of a group chat.
SERVER_GROUP: Final = "g.us"
#: Server part of a "hidden" privacy identifier. WhatsApp increasingly addresses
#: group participants by LID instead of phone number, so we must accept it.
SERVER_LID: Final = "lid"
#: Server part of a channel / newsletter.
SERVER_NEWSLETTER: Final = "newsletter"
#: Server part of the status broadcast pseudo-chat.
SERVER_BROADCAST: Final = "broadcast"

KNOWN_SERVERS: Final = frozenset(
    {SERVER_USER, SERVER_GROUP, SERVER_LID, SERVER_NEWSLETTER, SERVER_BROADCAST}
)

# Characters people habitually type inside phone numbers and which carry no
# meaning: spaces, NBSP, dashes (incl. en/em dash), dots, slashes, brackets.
_PHONE_NOISE = re.compile(r"[\s\u00a0\-\u2010-\u2015./()\[\]]+")
_ONLY_DIGITS = re.compile(r"^\d+$")


@dataclass(frozen=True)
class Jid:
    """A parsed WhatsApp address.

    ``user`` is the part before the ``@`` with any device/agent suffix removed,
    ``server`` the part after it.
    """

    user: str
    server: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.user}@{self.server}"

    @property
    def is_group(self) -> bool:
        return self.server == SERVER_GROUP

    @property
    def phone(self) -> str | None:
        """The E.164 phone number, when this JID is backed by one.

        Group JIDs and LIDs have no phone number and return ``None``.
        """
        if self.server == SERVER_USER and _ONLY_DIGITS.match(self.user):
            return "+" + self.user
        return None


def parse_jid(value: str) -> Jid:
    """Parse a string that is already known to be a JID.

    WhatsApp JIDs that identify a *device* carry suffixes the send APIs reject
    (``:12`` for the device id, ``_1`` for an agent). Both are stripped so that
    replying to a message whose sender is ``15550100:7@s.whatsapp.net``
    addresses the person instead of one of their phones.
    """
    raw = value.strip()
    user, _, server = raw.partition("@")
    server = server.lower()
    if not server:
        raise ConfigError(f"'{value}' is not a valid WhatsApp address (no '@' found).")
    if server not in KNOWN_SERVERS:
        raise ConfigError(
            f"'{value}' uses the unknown WhatsApp server '{server}'. "
            f"Expected one of: {', '.join(sorted(KNOWN_SERVERS))}."
        )
    user = user.split(":", 1)[0].split("_", 1)[0]
    if not user:
        raise ConfigError(f"'{value}' is not a valid WhatsApp address (empty user part).")
    return Jid(user=user, server=server)


def normalise_phone(value: str, default_country_code: str = "") -> str:
    """Reduce a human-typed phone number to bare international digits.

    WhatsApp addresses people by country code + national number with no ``+``
    and no leading zeros, e.g. ``15550100``. This accepts the formats people
    actually paste from a contact card:

    ``+1 555 0100`` / ``001-555-0100`` / ``(555) 0100``

    ``default_country_code`` (digits only, e.g. ``"1"``) is prefixed when the
    number is clearly national - that is, when the user gave no ``+`` and no
    ``00`` prefix. It is the tool's "Default country code" setting and exists so
    that a spreadsheet column of local numbers just works.

    :raises ConfigError: if the result is not a plausible phone number.
    """
    cleaned = _PHONE_NOISE.sub("", value.strip())
    if not cleaned:
        raise ConfigError("Empty phone number.")

    had_international_prefix = False
    if cleaned.startswith("+"):
        cleaned = cleaned[1:]
        had_international_prefix = True
    # A "+00" prefix is a typo, but a common one. Strip both rather than
    # leaving the zeros to be read as part of the country code.
    if cleaned.startswith("00"):
        cleaned = cleaned[2:]
        had_international_prefix = True

    if not _ONLY_DIGITS.match(cleaned):
        raise ConfigError(
            f"'{value}' is not a phone number: it still contains non-digits after "
            "removing spaces, dashes and brackets."
        )

    if not had_international_prefix and default_country_code:
        cc = _PHONE_NOISE.sub("", default_country_code).lstrip("+")
        if not _ONLY_DIGITS.match(cc):
            raise ConfigError(
                f"The default country code '{default_country_code}' is not numeric."
            )
        # A national number is often written with a trunk '0' (e.g. UK 07...,
        # DE 0170...). That zero is never part of the international form.
        cleaned = cc + cleaned.lstrip("0")

    # E.164 allows at most 15 digits; fewer than 6 cannot be a real number.
    if not 6 <= len(cleaned) <= 15:
        raise ConfigError(
            f"'{value}' does not look like a full international phone number "
            f"(got {len(cleaned)} digits, expected 6-15). "
            "Include the country code, or set 'Default country code' in the tool."
        )
    return cleaned


def looks_like_phone(value: str) -> bool:
    """True when ``value`` could be a phone number rather than a chat name."""
    cleaned = _PHONE_NOISE.sub("", value.strip())
    cleaned = cleaned[1:] if cleaned.startswith("+") else cleaned
    return bool(cleaned) and _ONLY_DIGITS.match(cleaned) is not None


def parse_destination(value: str, default_country_code: str = "") -> Jid | None:
    """Best-effort conversion of a user-supplied destination into a :class:`Jid`.

    Returns ``None`` when ``value`` is neither a JID nor a phone number, which
    means "this is a chat name - go ask the archive". Returning ``None`` rather
    than raising keeps the name-resolution fallback in one place (the caller).
    """
    text = value.strip()
    if not text:
        raise ConfigError("Empty destination: no chat id, phone number or chat name given.")
    if "@" in text:
        return parse_jid(text)
    if looks_like_phone(text):
        return Jid(user=normalise_phone(text, default_country_code), server=SERVER_USER)
    return None


