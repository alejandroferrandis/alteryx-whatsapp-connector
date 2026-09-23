"""Command line access to everything the tools do.

This exists for three audiences:

* **Support.** "Run ``doctor`` and paste the output" diagnoses most installation
  problems in one step, without asking a customer to build a workflow.
* **Setup.** Linking a device from a terminal is sometimes easier than from
  Designer, especially on a server.
* **Us.** Every code path the plugins use can be exercised here, which is how
  the connector gets tested against a real account without Designer in the loop.

Run it with the interpreter Designer uses so it sees the same bundled libraries::

    python -m whatsapp_core.cli doctor
    python -m whatsapp_core.cli link --profile default --phone +15550100
    python -m whatsapp_core.cli sync --profile default
    python -m whatsapp_core.cli send --profile default --to "+15550100" --message "hi"
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Sequence

from . import runner
from .config import MODE_ARCHIVE, MODE_SYNC, ConnectionSettings, InputSettings, OutputSettings
from .errors import WhatsAppError
from .profiles import Profile, default_data_root, list_profiles
from .sender import SendRequest, Sender
from .store import Store
from .version import __version__


def _mask_number(number: str | None) -> str:
    """Show enough of a phone number to identify it, not enough to use it."""
    digits = "".join(c for c in (number or "") if c.isdigit())
    if not digits:
        return ""
    return f"+{digits[:2]}…{digits[-2:]}"


def _log(message: str) -> None:
    print(message, flush=True)


def _connection(args: argparse.Namespace) -> ConnectionSettings:
    return ConnectionSettings(
        profile=args.profile,
        data_dir=getattr(args, "data_dir", "") or "",
        proxy_url=getattr(args, "proxy", "") or "",
        connect_timeout=getattr(args, "timeout", 60),
        device_name=getattr(args, "device_name", "Alteryx"),
    )


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check that everything the tools need is present and working."""
    ok = True
    print(f"WhatsApp connector for Alteryx  v{__version__}")
    print(f"Python                          {sys.version.split()[0]} ({sys.platform})")
    print(f"Data root                       {default_data_root()}")

    try:
        import neonize

        dll = Path(neonize.__file__).parent
        libs = sorted(p.name for p in dll.glob("neonize-*"))
        print(f"WhatsApp library                neonize at {dll}")
        print(f"Native core                     {', '.join(libs) or 'MISSING'}")
        if not libs:
            ok = False
    except Exception as exc:  # noqa: BLE001
        print(f"WhatsApp library                FAILED: {exc}")
        ok = False

    try:
        from neonize.client import NewClient  # noqa: F401

        print("Library import                  ok")
    except Exception as exc:  # noqa: BLE001
        print(f"Library import                  FAILED: {exc}")
        ok = False

    import shutil

    ffmpeg = shutil.which("ffmpeg") or (
        "not installed - video and audio files are sent as documents instead of "
        "playable messages. Nothing else is affected."
    )
    print(f"FFmpeg (optional)               {ffmpeg}")

    found = list_profiles(_data_dir(args))
    if not found:
        print("Profiles                        none yet (run 'link' to create one)")
    else:
        print("Profiles")
        for profile in found:
            state = "linked" if profile.is_linked else "NOT LINKED"
            # Masked because docs/07 tells people to attach this output to bug
            # reports, and diagnostics._redact strips the same number from
            # crash reports. Two outputs, one posture.
            number = _mask_number(profile.linked_number)
            print(f"  - {profile.name:<20} {state:<12} {number}")

    print()
    print("All good." if ok else "Problems found; see the FAILED lines above.")
    return 0 if ok else 1


def cmd_status(args: argparse.Namespace) -> int:
    info = runner.describe_profile(_connection(args))
    # Same reasoning as doctor: this output gets pasted into bug reports.
    for key in ("linked_number", "linked_jid"):
        if info.get(key):
            info[key] = _mask_number(str(info[key]))
    print(json.dumps(info, indent=2, sort_keys=True, default=str))
    return 0


def cmd_link(args: argparse.Namespace) -> int:
    connection = _connection(args)
    connection.link_phone = args.phone or ""
    connection.force_relink = args.force
    result = runner.link_device(connection, log=_log, wait_seconds=args.wait)
    if not result.success:
        print(result.message, file=sys.stderr)
        return 1
    return 0


def _data_dir(args: argparse.Namespace) -> str | None:
    """--data-dir, expanded the same way the Alteryx tools expand it.

    Profile.open only calls expanduser. Every path that goes through
    ConnectionSettings.resolved_data_dir also gets expandvars, so without this
    a --data-dir containing %VAR% sent `chats`, `prune` and `unlink` to a
    literally-named directory while `sync` and `send` used the real one.
    """
    raw = getattr(args, "data_dir", "") or ""
    if not raw:
        return None
    return os.path.expanduser(os.path.expandvars(raw))


def cmd_unlink(args: argparse.Namespace) -> int:
    profile = Profile.open(args.profile, _data_dir(args))
    if not profile.is_linked:
        print(f"{profile.describe()} - nothing to remove.")
        return 0
    if not args.yes:
        answer = input(
            f"Remove the local WhatsApp link for {profile.describe()}? "
            "The archive is kept. [y/N] "
        )
        if answer.strip().lower() not in {"y", "yes"}:
            print("Cancelled.")
            return 1
    profile.unlink_local()
    print(
        f'Local session for "{profile.name}" removed. The device may still appear '
        "on the phone under Linked devices; remove it there too."
    )
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    settings = InputSettings(
        connection=_connection(args),
        mode=MODE_SYNC,
        idle_timeout=args.idle,
        max_sync_seconds=args.max_seconds,
        only_new=False,
        max_records=0,
        emit_chats=False,
        download_media=not args.no_media,
        ignore_older_than_days=args.max_age_days,
        start_from_first_run=not args.include_backlog,
    )
    result = runner.read_messages(settings, log=_log)
    print(result.stats.summary())
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    settings = InputSettings(
        connection=_connection(args),
        mode=MODE_ARCHIVE,
        only_new=args.only_new,
        max_records=args.limit,
        chats=list(args.chat or []),
        include_own_messages=True,
    )
    result = runner.read_messages(settings, log=_log if args.verbose else (lambda _m: None))
    rows = result.messages
    if args.format == "json":
        print(json.dumps(
            [
                {
                    "MessageId": r.message_id, "ChatId": r.chat_id, "ChatName": r.chat_name,
                    "IsGroup": r.is_group, "SenderName": r.sender_name,
                    "FromMe": r.from_me, "Timestamp": r.timestamp.isoformat(),
                    "Body": r.body, "MessageType": r.message_type,
                    "MediaPath": r.media_path,
                }
                for r in rows
            ],
            indent=2, ensure_ascii=False,
        ))
    else:
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(
            ["Timestamp", "ChatName", "ChatId", "SenderName", "FromMe", "Type", "Body", "MediaPath"]
        )
        for r in rows:
            writer.writerow([
                r.timestamp.isoformat(), r.chat_name, r.chat_id, r.sender_name,
                r.from_me, r.message_type, r.body.replace("\n", " "), r.media_path,
            ])
    if args.mark_emitted:
        runner.mark_emitted(settings, result.emitted_keys)
    return 0


def cmd_chats(args: argparse.Namespace) -> int:
    profile = Profile.open(args.profile, _data_dir(args))
    with Store(profile.archive_db) as store:
        chats = store.list_chats()
    if not chats:
        print(
            "No chats known yet. Run 'sync' once so the connector can learn the "
            "chat directory from WhatsApp."
        )
        return 1
    width = max(len(c.name) for c in chats) if chats else 10
    print(f"{'NAME'.ljust(width)}  {'TYPE':<7}  CHAT ID")
    for chat in chats:
        print(f"{chat.name.ljust(width)}  {'group' if chat.is_group else 'direct':<7}  {chat.chat_id}")
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    settings = OutputSettings(
        connection=_connection(args),
        to_source="Fixed", to_fixed=args.to,
        message_source="Fixed", message_fixed=args.message or "",
        default_country_code=args.country_code or "",
        messages_per_minute=args.rate,
        verify_recipients=not args.no_verify,
    )
    request = SendRequest(
        row_index=1, to=args.to, body=args.message or "", attachment=args.file or ""
    )
    with Sender(settings, log=_log) as sender:
        # Connect first so a dead profile is one clear failure rather than a
        # row-level error, and run the recipient check the flag claims to
        # control - without this, --no-verify disabled something that never
        # happened.
        sender.connect()
        sender.verify([request])
        outcome = sender.send(request)
    if outcome.success:
        print(f"Sent to {outcome.chat_id} (message id {outcome.message_id}).")
        return 0
    print(outcome.error, file=sys.stderr)
    return 1


def cmd_prune(args: argparse.Namespace) -> int:
    profile = Profile.open(args.profile, _data_dir(args))
    with Store(profile.archive_db) as store:
        removed = store.prune(args.days)
        print(f"Removed {removed} message(s) older than {args.days} days.")
    return 0


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="whatsapp_core.cli",
        description="Manage the WhatsApp connector for Alteryx Designer.",
    )
    parser.add_argument("--version", action="version", version=__version__)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--profile", default="default", help="Profile name (default: default)")
    common.add_argument("--data-dir", default="", help="Override where profiles are stored")
    common.add_argument("--proxy", default="", help="Proxy URL, e.g. socks5://host:1080")
    common.add_argument("--timeout", type=int, default=60, help="Connection timeout in seconds")
    common.add_argument("--device-name", default="Alteryx",
                        help="Name shown on the phone under Linked devices")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", parents=[common], help="Check the installation")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("status", parents=[common], help="Show one profile's state")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("link", parents=[common], help="Link a WhatsApp device")
    p.add_argument("--phone", default="",
                   help="Number to link, e.g. +15550100. Omit for a QR code.")
    p.add_argument("--force", action="store_true", help="Replace an existing link")
    p.add_argument("--wait", type=int, default=180,
                   help="Seconds to wait for the code to be entered")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("unlink", parents=[common], help="Delete the local session")
    p.add_argument("--yes", action="store_true", help="Do not ask for confirmation")
    p.set_defaults(func=cmd_unlink)

    p = sub.add_parser("sync", parents=[common], help="Fetch new messages into the archive")
    p.add_argument("--idle", type=int, default=8, help="Stop after N quiet seconds")
    p.add_argument("--max-seconds", type=int, default=120, help="Hard limit on one sync")
    p.add_argument("--no-media", action="store_true", help="Do not download attachments")
    p.add_argument("--max-age-days", type=int, default=7,
                   help="Ignore messages older than this many days (0 = no limit)")
    p.add_argument("--include-backlog", action="store_true",
                   help="Also take messages from before this profile's first sync")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("read", parents=[common], help="Print archived messages")
    p.add_argument("--format", choices=["csv", "json"], default="csv")
    p.add_argument("--limit", type=int, default=50, help="0 for everything")
    p.add_argument("--chat", action="append", help="Chat id, number or name (repeatable)")
    p.add_argument("--only-new", action="store_true", help="Only rows never emitted before")
    p.add_argument("--mark-emitted", action="store_true",
                   help="Stamp the printed rows as emitted")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("chats", parents=[common], help="List known chats and their ids")
    p.set_defaults(func=cmd_chats)

    p = sub.add_parser("send", parents=[common], help="Send one message")
    p.add_argument("--to", required=True, help="Phone number, chat id, or chat name")
    p.add_argument("--message", default="", help="Text to send")
    p.add_argument("--file", default="", help="Path to a file to attach")
    p.add_argument("--country-code", default="", help="Default country code for bare numbers")
    p.add_argument("--rate", type=int, default=20, help="Messages per minute")
    p.add_argument("--no-verify", action="store_true",
                   help="Skip the 'is this number on WhatsApp' check")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("prune", parents=[common], help="Delete old archived messages")
    p.add_argument("--days", type=int, required=True, help="Keep messages newer than N days")
    p.set_defaults(func=cmd_prune)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except WhatsAppError as exc:
        # Expected failures already carry a fix; a traceback would only bury it.
        print(f"\n{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
