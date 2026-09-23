"""Unit tests for the parts of the connector that need no WhatsApp account.

Run them with the interpreter Designer uses, so the tests exercise the same
Python the tools will::

    "C:\\Program Files\\Alteryx\\bin\\Python\\python-3.13.11-embed-amd64\\python.exe" \\
        tests\\run_tests.py

Everything here runs offline: JID parsing, configuration coercion,
the archive's query and watermark behaviour, and message classification against
hand-built protobufs. The networking layer is covered by the CLI's ``doctor``
and ``sync`` commands against a real linked profile.
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make `python tests/test_x.py` work as well as tests/run_tests.py.
# The imports below need src/ - and, for the plugin tests, pyarrow and the SDK -
# on sys.path, which run_tests arranges at import time. Without this, running a
# test file directly fails on the first import, or silently collects nothing.
import pathlib as _pathlib
import sys as _sys

_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent))
import run_tests as _run_tests  # noqa: F401,E402

from whatsapp_core import config, jid, profiles, store
from whatsapp_core.errors import ConfigError, ProfileLockedError


class TestJid(unittest.TestCase):
    def test_parses_plain_user_jid(self):
        parsed = jid.parse_jid("15550100@s.whatsapp.net")
        self.assertEqual(parsed.user, "15550100")
        self.assertFalse(parsed.is_group)
        self.assertEqual(parsed.phone, "+15550100")

    def test_strips_device_and_agent_suffixes(self):
        # Senders in group messages arrive with a device id appended; sending to
        # that address fails, so it must be stripped.
        self.assertEqual(jid.parse_jid("15550100:7@s.whatsapp.net").user, "15550100")
        self.assertEqual(jid.parse_jid("15550100_1@s.whatsapp.net").user, "15550100")

    def test_group_jid(self):
        parsed = jid.parse_jid("120363000000000000@g.us")
        self.assertTrue(parsed.is_group)
        self.assertIsNone(parsed.phone)

    def test_rejects_unknown_server(self):
        with self.assertRaises(ConfigError):
            jid.parse_jid("123@example.com")

    def test_normalises_human_phone_formats(self):
        for raw in ("+1 555 0100", "001-555-0100", "+1 (555) 0100"):
            self.assertEqual(jid.normalise_phone(raw), "15550100", raw)

    def test_default_country_code_applies_only_to_national_numbers(self):
        self.assertEqual(jid.normalise_phone("5550100", "1"), "15550100")
        # A trunk zero is national notation and never part of the E.164 form.
        self.assertEqual(jid.normalise_phone("05550100", "1"), "15550100")
        # An explicit international number must not be prefixed again.
        self.assertEqual(jid.normalise_phone("+1 415 555 0100", "1"), "14155550100")

    def test_rejects_implausible_numbers(self):
        with self.assertRaises(ConfigError):
            jid.normalise_phone("12345")

    def test_destination_dispatch(self):
        self.assertEqual(str(jid.parse_destination("+15550100")), "15550100@s.whatsapp.net")
        self.assertEqual(str(jid.parse_destination("1203@g.us")), "1203@g.us")
        # A name is not resolvable here; None tells the caller to ask the store.
        self.assertIsNone(jid.parse_destination("Family"))


class TestConfigCoercion(unittest.TestCase):
    def test_bool_spellings(self):
        for value in ("True", "true", "1", "yes", "on"):
            self.assertTrue(config.as_bool(value), value)
        for value in ("False", "false", "0", "no", ""):
            self.assertFalse(config.as_bool(value), value)

    def test_bool_falls_back_rather_than_raising(self):
        self.assertTrue(config.as_bool("nonsense", default=True))

    def test_int_clamps_into_range(self):
        self.assertEqual(config.as_int("999", 8, minimum=1, maximum=600), 600)
        self.assertEqual(config.as_int("", 8, minimum=1, maximum=600), 8)

    def test_split_list_handles_mixed_separators(self):
        self.assertEqual(
            config.split_list("a, b;c\nd"), ["a", "b", "c", "d"]
        )

    def test_input_settings_defaults(self):
        settings = config.InputSettings.from_config({})
        self.assertEqual(settings.mode, config.MODE_SYNC)
        self.assertTrue(settings.only_new)
        self.assertEqual(settings.connection.profile, "default")

    def test_input_rejects_excluding_every_chat_type(self):
        with self.assertRaises(ConfigError) as caught:
            config.InputSettings.from_config(
                {"IncludeGroups": "False", "IncludeDirect": "False"}
            )
        self.assertIn("never return a row", str(caught.exception))

    def test_input_rejects_idle_longer_than_max(self):
        with self.assertRaises(ConfigError):
            config.InputSettings.from_config({"IdleTimeout": "60", "MaxSyncSeconds": "30"})

    def test_output_requires_a_mapped_column(self):
        with self.assertRaises(ConfigError) as caught:
            config.OutputSettings.from_config({"ToSource": "Field", "MessageSource": "Field"})
        self.assertIn("no column is selected", str(caught.exception))

    def test_output_link_run_skips_column_validation(self):
        # Linking has no incoming data, so unmapped columns must not block it.
        settings = config.OutputSettings.from_config(
            {"LinkDevice": "True", "LinkPhone": "+15550100"}
        )
        self.assertTrue(settings.connection.link_device)

    def test_output_rejects_bad_country_code(self):
        with self.assertRaises(ConfigError):
            config.OutputSettings.from_config(
                {"ToSource": "Fixed", "ToFixed": "x", "MessageSource": "Fixed",
                 "MessageFixed": "y", "DefaultCountryCode": "abc"}
            )


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wa-test-"))
        self.store = store.Store(self.tmp / "archive.sqlite3")

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _message(self, message_id: str, chat: str = "15550100@s.whatsapp.net",
                 when: datetime | None = None, is_group: bool = False,
                 from_me: bool = False) -> store.MessageRow:
        return store.MessageRow(
            message_id=message_id,
            chat_id=chat,
            is_group=is_group,
            from_me=from_me,
            timestamp=when or datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
            body=f"body {message_id}",
        )

    def test_insert_is_idempotent(self):
        rows = [self._message("A"), self._message("B")]
        self.assertEqual(self.store.add_messages(rows), 2)
        # WhatsApp re-delivers after a reconnect; a second sync must not
        # duplicate the tail of history.
        self.assertEqual(self.store.add_messages(rows), 0)
        self.assertEqual(len(self.store.query_messages()), 2)

    def test_same_id_in_different_chats_are_distinct(self):
        self.store.add_messages([self._message("A", chat="1@s.whatsapp.net")])
        self.store.add_messages([self._message("A", chat="2@s.whatsapp.net")])
        self.assertEqual(len(self.store.query_messages()), 2)

    def test_only_new_watermark(self):
        self.store.add_messages([self._message("A"), self._message("B")])
        first = self.store.query_messages(only_new=True)
        self.assertEqual(len(first), 2)
        self.store.mark_emitted([(r.chat_id, r.message_id) for r in first])
        self.assertEqual(self.store.query_messages(only_new=True), [])
        # Without the watermark the history is still there to re-read.
        self.assertEqual(len(self.store.query_messages(only_new=False)), 2)

    def test_date_range_and_ordering(self):
        base = datetime(2026, 9, 1, tzinfo=timezone.utc)
        self.store.add_messages([
            self._message("old", when=base),
            self._message("mid", when=base + timedelta(days=5)),
            self._message("new", when=base + timedelta(days=10)),
        ])
        rows = self.store.query_messages(
            date_from=base + timedelta(days=1), date_to=base + timedelta(days=9)
        )
        self.assertEqual([r.message_id for r in rows], ["mid"])
        # Oldest first, so a workflow replies in conversation order.
        every = self.store.query_messages()
        self.assertEqual([r.message_id for r in every], ["old", "mid", "new"])

    def test_group_and_direct_filters(self):
        self.store.add_messages([
            self._message("g", chat="120@g.us", is_group=True),
            self._message("d"),
        ])
        self.assertEqual(
            [r.message_id for r in self.store.query_messages(include_direct=False)], ["g"]
        )
        self.assertEqual(
            [r.message_id for r in self.store.query_messages(include_groups=False)], ["d"]
        )

    def test_own_messages_excluded_on_request(self):
        self.store.add_messages([self._message("mine", from_me=True), self._message("theirs")])
        rows = self.store.query_messages(include_own=False)
        self.assertEqual([r.message_id for r in rows], ["theirs"])

    def test_chat_directory_keeps_a_known_name(self):
        self.store.upsert_chats([store.ChatRow(chat_id="120@g.us", name="Family", is_group=True)])
        # A later sync that reports the group without a subject must not blank it.
        self.store.upsert_chats([store.ChatRow(chat_id="120@g.us", name="", is_group=True)])
        self.assertEqual(self.store.list_chats()[0].name, "Family")

    def test_resolve_chat_name_is_case_insensitive_and_lists_ambiguity(self):
        self.store.upsert_chats([
            store.ChatRow(chat_id="1@g.us", name="Team", is_group=True),
            store.ChatRow(chat_id="2@g.us", name="team", is_group=True),
        ])
        self.assertEqual(len(self.store.resolve_chat_name("TEAM")), 2)
        self.assertEqual(self.store.resolve_chat_name("nope"), [])

    def test_limit_applies_after_ordering(self):
        base = datetime(2026, 9, 1, tzinfo=timezone.utc)
        self.store.add_messages(
            [self._message(str(i), when=base + timedelta(minutes=i)) for i in range(5)]
        )
        rows = self.store.query_messages(limit=2)
        self.assertEqual([r.message_id for r in rows], ["0", "1"])

    def test_prune_removes_only_old_rows(self):
        now = datetime.now(timezone.utc)
        self.store.add_messages([
            self._message("old", when=now - timedelta(days=90)),
            self._message("recent", when=now),
        ])
        self.assertEqual(self.store.prune(30), 1)
        self.assertEqual([r.message_id for r in self.store.query_messages()], ["recent"])


class TestProfiles(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wa-profile-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_creates_directory_layout(self):
        profile = profiles.Profile.open("acme", self.tmp)
        self.assertTrue(profile.media_dir.is_dir())
        self.assertFalse(profile.is_linked)
        self.assertIn("not linked", profile.describe())

    def test_rejects_path_traversal_in_name(self):
        with self.assertRaises(ConfigError):
            profiles.Profile.open("../escape", self.tmp)

    def test_metadata_round_trip(self):
        profile = profiles.Profile.open("acme", self.tmp)
        profile.update_metadata(linked_number="+15550100")
        reopened = profiles.Profile.open("acme", self.tmp)
        self.assertEqual(reopened.linked_number, "+15550100")

    def test_lock_is_exclusive(self):
        profile = profiles.Profile.open("acme", self.tmp)
        with profile.lock():
            with self.assertRaises(ProfileLockedError):
                with profiles.Profile.open("acme", self.tmp).lock():
                    pass
        # Released on exit, so the next run can take it.
        with profile.lock():
            pass

    def test_stale_lock_from_a_dead_process_is_reclaimed(self):
        profile = profiles.Profile.open("acme", self.tmp)
        import json
        import socket

        profile.lock_file.write_text(
            json.dumps({"pid": 999999, "host": socket.gethostname()}), encoding="utf-8"
        )
        with profile.lock():
            pass  # Must not raise: a crashed workflow cannot wedge a profile.

    def test_list_profiles(self):
        profiles.Profile.open("one", self.tmp)
        profiles.Profile.open("two", self.tmp)
        self.assertEqual([p.name for p in profiles.list_profiles(self.tmp)], ["one", "two"])



class TestConnectionStateFlags(unittest.TestCase):
    """Regression cover for reading neonize's connection-state flags.

    ``is_logged_in`` / ``is_connected`` are ``@property`` in neonize 0.5.2 and
    were methods in earlier versions. Getting this wrong is expensive because it
    fails silently: the exception is swallowed, the flag reads False forever,
    and a perfectly good connection is reported as a timeout.
    """

    @staticmethod
    def _session(client):
        from whatsapp_core.client import WhatsAppSession

        session = WhatsAppSession.__new__(WhatsAppSession)
        session._client = client
        return session

    def test_reads_a_property(self):
        class AsProperty:
            @property
            def is_logged_in(self):
                return True

            @property
            def is_connected(self):
                return False

        session = self._session(AsProperty())
        self.assertTrue(session.is_logged_in())
        self.assertFalse(session.is_connected())

    def test_reads_a_method(self):
        class AsMethod:
            def is_logged_in(self):
                return True

            def is_connected(self):
                return True

        session = self._session(AsMethod())
        self.assertTrue(session.is_logged_in())
        self.assertTrue(session.is_connected())

    def test_a_raising_accessor_is_false_not_an_exception(self):
        class Broken:
            @property
            def is_logged_in(self):
                raise RuntimeError("the Go side is not up yet")

            def is_connected(self):
                raise RuntimeError("boom")

        session = self._session(Broken())
        self.assertFalse(session.is_logged_in())
        self.assertFalse(session.is_connected())

    def test_missing_accessor_is_false(self):
        session = self._session(object())
        self.assertFalse(session.is_logged_in())
        self.assertFalse(session.is_connected())


class TestChatDirectoryOrdering(unittest.TestCase):
    """The Chats anchor exists to make chat ids findable.

    On a real account most directory entries are nameless LID identifiers. If
    they sort alongside everything else they bury the handful of groups, which
    are the entries people actually come looking for.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wa-chats-"))
        self.store = store.Store(self.tmp / "archive.sqlite3")

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_groups_and_named_chats_outrank_nameless_ones(self):
        self.store.upsert_chats([
            store.ChatRow(chat_id="100000000000002@lid", name=""),
            store.ChatRow(chat_id="100000000000003@lid", name=""),
            store.ChatRow(chat_id="15550100@s.whatsapp.net", name="Ana Example"),
            store.ChatRow(chat_id="120363040000000001@g.us", name="Ops Team", is_group=True),
        ])
        order = [c.chat_id for c in self.store.list_chats()]
        self.assertEqual(order[0], "120363040000000001@g.us", "the group must come first")
        self.assertEqual(order[1], "15550100@s.whatsapp.net", "then the named contact")
        self.assertEqual(set(order[2:]), {"100000000000002@lid", "100000000000003@lid"})

    def test_recent_activity_still_wins(self):
        recent = datetime(2026, 9, 20, tzinfo=timezone.utc)
        self.store.upsert_chats([
            store.ChatRow(chat_id="120363040000000001@g.us", name="Ops Team", is_group=True),
            store.ChatRow(chat_id="15550100@s.whatsapp.net", name="Test Contact",
                          last_message=recent),
        ])
        order = [c.chat_id for c in self.store.list_chats()]
        self.assertEqual(order[0], "15550100@s.whatsapp.net")


class TestTimestampHandling(unittest.TestCase):
    """Regression cover for the crash that killed the tool in Designer.

    A message whose Timestamp was in milliseconds made
    ``datetime.fromtimestamp`` raise ``OSError: [Errno 22] Invalid argument``
    on Windows, which surfaced as::

        The WhatsApp Input tool failed unexpectedly: [Errno 22] Invalid argument

    One unusual message must never cost a workflow its whole batch.
    """

    def test_seconds_are_unchanged(self):
        from whatsapp_core.timeutil import to_datetime

        self.assertEqual(
            to_datetime(1788259800),
            datetime(2026, 9, 1, 10, 50, tzinfo=timezone.utc),
        )

    def test_milliseconds_are_rescaled(self):
        from whatsapp_core.timeutil import to_datetime

        # This is the value that crashed the tool: as seconds it is year 57,700.
        self.assertEqual(
            to_datetime(1788259800000),
            datetime(2026, 9, 1, 10, 50, tzinfo=timezone.utc),
        )

    def test_microseconds_and_nanoseconds_are_rescaled(self):
        from whatsapp_core.timeutil import to_datetime

        expected = datetime(2026, 9, 1, 10, 50, tzinfo=timezone.utc)
        self.assertEqual(to_datetime(1788259800_000_000), expected)
        self.assertEqual(to_datetime(1788259800_000_000_000), expected)

    def test_garbage_never_raises(self):
        from whatsapp_core.timeutil import EPOCH, to_datetime

        for value in (None, "", "not a number", object(), float("nan")):
            self.assertIsInstance(to_datetime(value), datetime)
        self.assertEqual(to_datetime(0), EPOCH)
        self.assertEqual(to_datetime(None), EPOCH)

    def test_extremes_are_clamped_not_raised(self):
        from whatsapp_core.timeutil import to_datetime

        for value in (2**63 - 1, -(2**63), 253402300800, -99999999999):
            result = to_datetime(value)
            self.assertIsInstance(result, datetime)
            self.assertIsNotNone(result.tzinfo)

    def test_round_trip_through_the_archive(self):
        from whatsapp_core.timeutil import to_datetime

        tmp = Path(tempfile.mkdtemp(prefix="wa-ts-"))
        try:
            with store.Store(tmp / "a.sqlite3") as st:
                # A message that arrived with a millisecond timestamp.
                st.add_messages([
                    store.MessageRow(
                        message_id="ms", chat_id="1@s.whatsapp.net",
                        timestamp=to_datetime(1788259800000), body="hi",
                    )
                ])
                row = st.query_messages()[0]
                self.assertEqual(row.timestamp.year, 2026)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_epoch_helpers_never_raise(self):
        from whatsapp_core.timeutil import to_epoch_seconds

        self.assertIsNone(to_epoch_seconds(None))
        self.assertEqual(to_epoch_seconds(datetime(1970, 1, 1, tzinfo=timezone.utc)), 0)
        # Naive datetimes are treated as UTC rather than rejected.
        self.assertIsInstance(to_epoch_seconds(datetime(2026, 9, 1)), int)


class TestDateRangeBounds(unittest.TestCase):
    """The date filter silently swallowed a message that arrived the same day.

    ``to 2026-09-23`` was read as ``<= 2026-09-23 00:00:00``, so selecting
    "today to today" excluded all of today. An inclusive end date has to mean
    the end of that day.
    """

    def test_end_date_covers_the_whole_day(self):
        start = config.as_datetime("2026-09-23", "from")
        end = config.as_datetime("2026-09-23", "to", end_of_day=True)
        self.assertLess(start, end)
        # A message during that local day must fall inside the window.
        midday = datetime(2026, 9, 23, 12, 0).astimezone(timezone.utc)
        self.assertLessEqual(start, midday)
        self.assertGreaterEqual(end, midday)
        # Nearly 24 hours apart, whatever the local offset.
        self.assertGreater((end - start).total_seconds(), 86399)

    def test_start_date_is_the_first_instant(self):
        start = config.as_datetime("2026-09-23", "from")
        local_midnight = datetime(2026, 9, 23, 0, 0).astimezone(timezone.utc)
        self.assertEqual(start, local_midnight)

    def test_explicit_times_are_taken_as_given(self):
        value = config.as_datetime("2026-09-23 14:30:00", "to", end_of_day=True)
        expected = datetime(2026, 9, 23, 14, 30).astimezone(timezone.utc)
        self.assertEqual(value, expected)

    def test_results_are_always_utc_aware(self):
        for text in ("2026-09-23", "2026-09-23 08:00:00"):
            value = config.as_datetime(text, "x")
            self.assertEqual(value.tzinfo, timezone.utc)

    def test_range_is_ignored_unless_switched_on(self):
        # Alteryx's date widget pre-fills today, so an untouched pair of fields
        # must not filter anything.
        settings = config.InputSettings.from_config(
            {"DateFrom": "2026-09-23", "DateTo": "2026-09-23"}
        )
        self.assertFalse(settings.use_date_range)
        self.assertIsNone(settings.date_from)
        self.assertIsNone(settings.date_to)

    def test_range_applies_when_switched_on(self):
        settings = config.InputSettings.from_config(
            {"UseDateRange": "True", "DateFrom": "2026-09-23", "DateTo": "2026-09-23"}
        )
        self.assertTrue(settings.use_date_range)
        self.assertIsNotNone(settings.date_from)
        self.assertGreater(settings.date_to, settings.date_from)

    def test_same_day_range_finds_a_message_from_that_day(self):
        """End to end: the exact scenario that returned zero rows."""
        tmp = Path(tempfile.mkdtemp(prefix="wa-dates-"))
        try:
            settings = config.InputSettings.from_config(
                {"UseDateRange": "True", "DateFrom": "2026-09-23", "DateTo": "2026-09-23"}
            )
            with store.Store(tmp / "a.sqlite3") as st:
                arrived = datetime(2026, 9, 23, 9, 41).astimezone(timezone.utc)
                st.add_messages([
                    store.MessageRow(message_id="today", chat_id="1@s.whatsapp.net",
                                     timestamp=arrived, body="hello"),
                ])
                rows = st.query_messages(
                    date_from=settings.date_from, date_to=settings.date_to
                )
                self.assertEqual([r.message_id for r in rows], ["today"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestDeviceIdentity(unittest.TestCase):
    """The linked device must identify itself as the connector.

    ``DeviceProps`` moved between protobuf modules, so importing it from the
    wrong one failed silently and the device registered as ``os="Neonize"`` on
    a SAFARI platform. That is both a wrong name under *Linked devices* and an
    unrecognised client string for WhatsApp to judge the session by.
    """

    @staticmethod
    def _session(device_name="Alteryx"):
        from whatsapp_core.client import WhatsAppSession

        session = WhatsAppSession.__new__(WhatsAppSession)
        session.device_name = device_name
        session.log = lambda _m: None
        return session

    def _skip_without_library(self):
        try:
            import neonize  # noqa: F401
        except Exception:  # noqa: BLE001
            self.skipTest("neonize is not importable in this environment")

    def test_device_props_are_built(self):
        self._skip_without_library()
        props = self._session("Alteryx PROD")._device_props()
        self.assertIsNotNone(props, "falling back to None gives the device a library name")
        self.assertEqual(props.os, "Alteryx PROD")

    def test_platform_is_a_desktop_companion(self):
        self._skip_without_library()
        props = self._session()._device_props()
        self.assertEqual(props.platformType, props.DESKTOP)

    def test_blank_name_falls_back_to_the_product_name(self):
        self._skip_without_library()
        self.assertEqual(self._session("")._device_props().os, "Alteryx")

    def test_never_identifies_as_the_underlying_library(self):
        self._skip_without_library()
        self.assertNotIn("neonize", self._session()._device_props().os.lower())


class TestIngestFloor(unittest.TestCase):
    """How far back a sync is allowed to reach.

    WhatsApp delivers an offline device's whole backlog on reconnect, so a
    workflow paused over a holiday comes back to a flood. Two limits guard
    against that, and they have to compose without surprising anyone.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wa-floor-"))
        self.store = store.Store(self.tmp / "archive.sqlite3")

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _settings(**kw):
        return config.InputSettings.from_config(kw)

    def _floor(self, profile=None, **kw):
        from whatsapp_core.runner import resolve_ingest_floor

        return resolve_ingest_floor(self.store, self._settings(**kw), profile)

    @staticmethod
    def _profile_linked(when):
        """A stand-in profile that reports ``when`` as its link time.

        resolve_ingest_floor reads exactly one thing off a profile, so a stub
        keeps these tests away from the filesystem.
        """

        class _Stub:
            def metadata(self):
                if when is None:
                    return {}
                if isinstance(when, str):
                    return {"linked_at": when}
                return {"linked_at": when.strftime("%Y-%m-%dT%H:%M:%SZ")}

        return _Stub()

    def test_the_mark_is_when_the_device_was_linked(self):
        linked = datetime.now(timezone.utc) - timedelta(hours=2)
        floor = self._floor(self._profile_linked(linked),
                            StartFromFirstRun="True", IgnoreOlderThanDays="0")
        self.assertLess(abs((floor - linked).total_seconds()), 2)

    def test_a_message_sent_before_the_first_run_but_after_linking_is_kept(self):
        """The bug this guards: link, send a test message, run - nothing.

        Seeding the mark during the first sync put it *after* the message, so
        the message the person had just sent themselves to check the tool was
        discarded as too old. It is the first thing anyone does, and it made a
        working connector look broken.
        """
        linked = datetime.now(timezone.utc) - timedelta(minutes=30)
        sent = linked + timedelta(minutes=5)
        floor = self._floor(self._profile_linked(linked), StartFromFirstRun="True",
                            IgnoreOlderThanDays="0")
        self.assertLess(floor, sent, "a message sent after linking must survive")

    def test_anything_from_before_the_link_is_still_excluded(self):
        linked = datetime.now(timezone.utc) - timedelta(minutes=30)
        floor = self._floor(self._profile_linked(linked), StartFromFirstRun="True",
                            IgnoreOlderThanDays="0")
        self.assertGreater(floor, linked - timedelta(minutes=1))

    def test_a_profile_with_no_link_time_falls_back_to_now(self):
        # Profiles written by an earlier build have no linked_at; they must
        # keep working, just with the old start-at-first-sync behaviour.
        before = datetime.now(timezone.utc)
        floor = self._floor(self._profile_linked(None), StartFromFirstRun="True",
                            IgnoreOlderThanDays="0")
        self.assertGreaterEqual(floor, before - timedelta(seconds=5))

    def test_an_unreadable_link_time_falls_back_rather_than_raising(self):
        floor = self._floor(self._profile_linked("not a timestamp"),
                            StartFromFirstRun="True", IgnoreOlderThanDays="0")
        self.assertIsNotNone(floor)

    def test_first_run_marks_the_start_of_time(self):
        before = datetime.now(timezone.utc)
        floor = self._floor(StartFromFirstRun="True", IgnoreOlderThanDays="0")
        self.assertIsNotNone(floor)
        self.assertGreaterEqual(floor, before - timedelta(seconds=5))

    def test_the_mark_is_stable_across_runs(self):
        first = self._floor(StartFromFirstRun="True", IgnoreOlderThanDays="0")
        second = self._floor(StartFromFirstRun="True", IgnoreOlderThanDays="0")
        self.assertEqual(first, second, "the start-of-time mark must not drift")

    def test_the_mark_lives_in_the_archive_so_it_survives_a_relink(self):
        self._floor(StartFromFirstRun="True")
        self.assertTrue(self.store.get_meta("first_sync_utc"))

    def test_day_window_is_rolling(self):
        floor = self._floor(StartFromFirstRun="False", IgnoreOlderThanDays="7")
        expected = datetime.now(timezone.utc) - timedelta(days=7)
        self.assertLess(abs((floor - expected).total_seconds()), 5)

    def test_the_later_of_the_two_limits_wins(self):
        # A profile whose first sync was long ago, with a tight day window:
        # the day window is later, so it governs.
        old = int((datetime.now(timezone.utc) - timedelta(days=90)).timestamp())
        self.store.set_meta("first_sync_utc", str(old))
        floor = self._floor(StartFromFirstRun="True", IgnoreOlderThanDays="7")
        self.assertGreater(floor, datetime.now(timezone.utc) - timedelta(days=8))

        # And the other way round: a recent first sync beats a wide window.
        recent = int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp())
        self.store.set_meta("first_sync_utc", str(recent))
        floor = self._floor(StartFromFirstRun="True", IgnoreOlderThanDays="365")
        self.assertGreater(floor, datetime.now(timezone.utc) - timedelta(hours=2))

    def test_both_limits_off_means_no_floor(self):
        self.assertIsNone(
            self._floor(StartFromFirstRun="False", IgnoreOlderThanDays="0")
        )

    def test_unticking_start_from_first_run_lets_the_backlog_in(self):
        # The mark is still recorded, but no longer applied - so a user who
        # wants the history can have it without resetting the profile.
        self._floor(StartFromFirstRun="True")
        self.assertTrue(self.store.get_meta("first_sync_utc"))
        self.assertIsNone(
            self._floor(StartFromFirstRun="False", IgnoreOlderThanDays="0")
        )

    def test_defaults_are_protective(self):
        settings = config.InputSettings.from_config({})
        self.assertTrue(settings.start_from_first_run)
        self.assertEqual(settings.ignore_older_than_days, 7)

    def test_the_floor_never_deletes_what_is_already_archived(self):
        """It limits ingestion only. Tightening it must not lose history."""
        old_message = datetime.now(timezone.utc) - timedelta(days=200)
        self.store.add_messages([
            store.MessageRow(message_id="old", chat_id="1@s.whatsapp.net",
                             timestamp=old_message, body="from long ago"),
        ])
        self._floor(StartFromFirstRun="True", IgnoreOlderThanDays="1")
        self.assertEqual(len(self.store.query_messages()), 1)


class TestRevokedLinkRecovery(unittest.TestCase):
    """Following the error's own advice has to actually work.

    LoggedOutError tells the user to tick 'Link this device' and run. That was
    useless while a dead session file sat on disk: linking saw the file, decided
    the profile was already linked, and returned without doing anything. The
    user did as instructed, nothing happened, and there was no way to tell why.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wa-revoked-"))
        self.profile = profiles.Profile.open("acme", self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _link(self):
        self.profile.session_db.write_bytes(b"pretend device session")

    def _decide(self, force=False):
        from whatsapp_core.runner import should_discard_existing_link

        return should_discard_existing_link(self.profile, force)

    def test_revoked_link_is_discarded_without_extra_ticking(self):
        self._link()
        self.profile.update_metadata(revoked_at="2026-09-23T10:00:00Z")
        discard, why = self._decide(force=False)
        self.assertTrue(discard, "a revoked link must be replaced automatically")
        self.assertIn("revoked by WhatsApp", why)

    def test_healthy_link_is_left_alone(self):
        self._link()
        discard, _ = self._decide(force=False)
        self.assertFalse(discard, "a working link is never discarded unasked")

    def test_healthy_link_is_discarded_when_asked(self):
        self._link()
        discard, why = self._decide(force=True)
        self.assertTrue(discard)
        self.assertIn("as requested", why)

    def test_nothing_to_discard_when_never_linked(self):
        discard, why = self._decide(force=True)
        self.assertFalse(discard)
        self.assertEqual(why, "")

    def test_relinking_clears_the_revoked_mark(self):
        """Otherwise every later link would keep throwing the session away."""
        self._link()
        self.profile.update_metadata(revoked_at="2026-09-23T10:00:00Z")
        self.profile.unlink_local()
        self.assertEqual(self.profile.metadata().get("revoked_at"), None)


class TestLockOwnership(unittest.TestCase):
    """Only the holder of a lock may release it.

    Releasing used to happen whenever a caller exited, whether or not it had
    ever acquired. A tool that timed out waiting therefore deleted the lock
    file belonging to the tool that held it, and a third process could then
    open the same WhatsApp device session - the corruption the lock exists to
    prevent, caused by the lock itself.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wa-lockown-"))
        self.profile = profiles.Profile.open("acme", self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _held_by_a_live_process(self):
        import json
        import os
        import socket

        self.profile.lock_file.write_text(
            json.dumps({"pid": os.getpid(), "host": socket.gethostname()}),
            encoding="utf-8",
        )

    def test_a_failed_acquire_does_not_release_someone_elses_lock(self):
        self._held_by_a_live_process()
        loser = self.profile.lock(timeout=0)
        with self.assertRaises(ProfileLockedError):
            loser.__enter__()
        loser.__exit__(None, None, None)
        self.assertTrue(
            self.profile.lock_file.exists(),
            "the holder's lock must survive another process giving up",
        )

    def test_exiting_without_ever_entering_is_harmless(self):
        self._held_by_a_live_process()
        self.profile.lock(timeout=0).__exit__(None, None, None)
        self.assertTrue(self.profile.lock_file.exists())

    def test_the_holder_still_releases_normally(self):
        lock = self.profile.lock(timeout=0)
        lock.__enter__()
        self.assertTrue(self.profile.lock_file.exists())
        lock.__exit__(None, None, None)
        self.assertFalse(self.profile.lock_file.exists())

    def test_double_release_is_safe(self):
        lock = self.profile.lock(timeout=0)
        lock.__enter__()
        lock.__exit__(None, None, None)
        # Sender's error path can release twice; it must not then delete a
        # lock a later process has taken in the meantime.
        self._held_by_a_live_process()
        lock.__exit__(None, None, None)
        self.assertTrue(self.profile.lock_file.exists())


class TestMediaPathIsRecorded(unittest.TestCase):
    """A downloaded file must end up attached to its row.

    Media is fetched after the drain, by which time the row has usually been
    flushed already. Writing the path with add_messages looked right and did
    nothing - INSERT OR IGNORE skips a row that exists - so the file landed on
    disk, the Results pane counted it, and MediaPath stayed empty. It only
    showed up on syncs past the flush threshold.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wa-media-"))
        self.store = store.Store(self.tmp / "archive.sqlite3")

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _row(self, message_id):
        return store.MessageRow(
            message_id=message_id, chat_id="1@s.whatsapp.net", has_media=True,
            timestamp=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )

    def test_path_lands_on_a_row_written_earlier(self):
        rows = [self._row(f"m{i}") for i in range(3)]
        self.store.add_messages(rows)          # flushed during the drain
        for r in rows:                          # download happens afterwards
            r.media_path = rf"C:\media\{r.message_id}.jpg"
            r.media_size = 1234

        self.assertEqual(self.store.attach_media(rows), 3)
        stored = {r.message_id: r for r in self.store.query_messages(include_own=True)}
        for r in rows:
            self.assertEqual(stored[r.message_id].media_path, r.media_path)
            self.assertEqual(stored[r.message_id].media_size, 1234)

    def test_rows_without_a_download_are_left_alone(self):
        row = self._row("nofile")
        self.store.add_messages([row])
        self.assertEqual(self.store.attach_media([row]), 0)

    def test_unreadable_timestamp_is_not_silently_redated(self):
        """timeutil promises a fallback that is obviously not real."""
        from whatsapp_core.timeutil import EPOCH

        self.store.add_messages([
            store.MessageRow(message_id="bad", chat_id="1@s.whatsapp.net",
                             timestamp=EPOCH, body="unreadable date"),
        ])
        kept = self.store.query_messages(include_own=True)[0]
        self.assertEqual(kept.timestamp.year, 1970,
                         "epoch 0 is falsy; it must not become 'now'")



class TestIngestFloorSeeding(unittest.TestCase):
    """How the start-of-time mark is chosen, and when it moves."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wa-seed-"))
        self.store = store.Store(self.tmp / "archive.sqlite3")

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _floor(self, profile=None, **kw):
        from whatsapp_core.runner import resolve_ingest_floor

        return resolve_ingest_floor(self.store, config.InputSettings.from_config(kw),
                                    profile)

    @staticmethod
    def _profile(linked_at):
        class _Stub:
            def metadata(self):
                return {} if linked_at is None else {"linked_at": linked_at}

        return _Stub()

    @staticmethod
    def _stamp(when):
        return when.strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_relinking_moves_the_mark_forward(self):
        """A new link is a new device, and gets a new start of time.

        unlink_local keeps the archive, so the previous device's mark is still
        there. Honouring it would put the floor a year in the past and let in
        exactly the history the setting exists to keep out - the mark has to
        move forward, never back.
        """
        old_link = datetime.now(timezone.utc) - timedelta(days=400)
        self.store.set_meta("first_sync_utc", str(int(old_link.timestamp())))

        new_link = datetime.now(timezone.utc) - timedelta(minutes=2)
        floor = self._floor(self._profile(self._stamp(new_link)),
                            StartFromFirstRun="True", IgnoreOlderThanDays="0")
        self.assertGreater(floor, datetime.now(timezone.utc) - timedelta(hours=1))

    def test_the_mark_never_moves_backwards(self):
        # A linked_at *older* than the recorded mark must not lower the floor.
        recorded = datetime.now(timezone.utc) - timedelta(days=1)
        self.store.set_meta("first_sync_utc", str(int(recorded.timestamp())))
        floor = self._floor(
            self._profile(self._stamp(datetime.now(timezone.utc) - timedelta(days=90))),
            StartFromFirstRun="True", IgnoreOlderThanDays="0")
        self.assertLess(abs((floor - recorded).total_seconds()), 2)

    def test_a_corrupt_mark_is_reseeded_rather_than_trusted(self):
        """to_datetime returns the epoch for junk, which would disable the floor.

        Silently reading a damaged value as 1970 turns "ignore old messages"
        into "import everything ever" at the one moment the protection matters.
        """
        self.store.set_meta("first_sync_utc", "not-a-number")
        floor = self._floor(StartFromFirstRun="True", IgnoreOlderThanDays="0")
        self.assertGreater(floor, datetime.now(timezone.utc) - timedelta(minutes=5))

    def test_the_log_only_claims_a_link_time_when_it_has_one(self):
        said = []
        from whatsapp_core.runner import resolve_ingest_floor

        resolve_ingest_floor(self.store, config.InputSettings.from_config(
            {"StartFromFirstRun": "True"}), self._profile(None), log=said.append)
        self.assertTrue(said)
        self.assertNotIn("when this device was linked", " ".join(said))

    def test_the_log_does_say_so_when_it_is_a_link_time(self):
        said = []
        from whatsapp_core.runner import resolve_ingest_floor

        linked = datetime.now(timezone.utc) - timedelta(minutes=5)
        resolve_ingest_floor(self.store, config.InputSettings.from_config(
            {"StartFromFirstRun": "True"}), self._profile(self._stamp(linked)),
            log=said.append)
        self.assertIn("when this device was linked", " ".join(said))


class TestChatDirectoryNames(unittest.TestCase):
    """Which of two competing names for a chat wins."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wa-names-"))
        self.store = store.Store(self.tmp / "archive.sqlite3")

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_push_name_does_not_overwrite_a_contact_name(self):
        """The directory's name comes from contacts and should stay there.

        A sender's push name is whatever they typed into their own handset.
        Letting it win turned "Alice Smith" into "ally" on the next sync that
        happened to include one of her messages - and the Output tool's
        "send by chat name" then stopped matching what the user had written.
        """
        jid = "15550111@s.whatsapp.net"
        self.store.upsert_chats([store.ChatRow(chat_id=jid, name="Alice Smith")])
        self.store.upsert_chats([store.ChatRow(chat_id=jid, name="ally")],
                                keep_known_name=True)
        row = [c for c in self.store.list_chats() if c.chat_id == jid][0]
        self.assertEqual(row.name, "Alice Smith")

    def test_a_push_name_still_fills_an_empty_slot(self):
        # An unknown number is exactly where the push name earns its place.
        jid = "15550122@s.whatsapp.net"
        self.store.upsert_chats([store.ChatRow(chat_id=jid, name="ally")],
                                keep_known_name=True)
        row = [c for c in self.store.list_chats() if c.chat_id == jid][0]
        self.assertEqual(row.name, "ally")


class TestArchiveVersionGuard(unittest.TestCase):
    def test_a_future_archive_raises_inside_the_error_hierarchy(self):
        """Outside it, the plugins report an upgrade prompt as a crash."""
        from whatsapp_core.errors import WhatsAppError

        tmp = Path(tempfile.mkdtemp(prefix="wa-ver-"))
        try:
            with store.Store(tmp / "archive.sqlite3") as db:
                db.set_meta("schema_version", "99")
            with self.assertRaises(WhatsAppError):
                store.Store(tmp / "archive.sqlite3").close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestDateRangeParsing(unittest.TestCase):
    def test_an_unreadable_date_is_ignored_while_the_range_is_off(self):
        """The panel keeps its date widgets populated when the box is unticked.

        Parsing them anyway meant a stored value from an older or hand-edited
        workflow failed a run that does not use dates at all.
        """
        settings = config.InputSettings.from_config(
            {"UseDateRange": "False", "DateFrom": "23/09/2026"})
        self.assertIsNone(settings.date_from)

    def test_the_same_value_is_still_rejected_when_the_range_is_on(self):
        with self.assertRaises(ConfigError):
            config.InputSettings.from_config(
                {"UseDateRange": "True", "DateFrom": "23/09/2026"})



class TestMediaFilenames(unittest.TestCase):
    """Where a downloaded attachment is written, and what that name has to be."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wa-media-"))
        self.profile = profiles.Profile.open("default", self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _download(self, chat_id, message_id, payload):
        from whatsapp_core import runner

        class _Payload:
            """Enough protobuf surface for messages.unwrap to give up cleanly."""

            def HasField(self, _name):
                return False

        class _Client:
            def download_any(self, _message, target):
                Path(target).write_bytes(payload)

        class _Session:
            _client = _Client()

        class _Event:
            # unwrap() walks a protobuf looking for the inner payload; a bare
            # object has no HasField, and the failure would be swallowed by the
            # "one bad file must not fail the sync" guard, quietly turning this
            # into a test that asserts nothing.
            Message = _Payload()

        row = store.MessageRow(
            message_id=message_id, chat_id=chat_id, has_media=True,
            message_type="image", media_mime="image/jpeg",
            timestamp=datetime.now(timezone.utc),
        )
        runner._download_media(
            _Session(), self.profile, row, _Event(),
            config.InputSettings.from_config({}), runner.SyncStats(), log=lambda _m: None,
        )
        return row

    def test_the_same_id_in_two_chats_gets_two_files(self):
        """WhatsApp only promises a message id is unique within its chat.

        Naming the file after the id alone meant the second chat's download saw
        a file already on disk, skipped it, and pointed its row at the first
        chat's picture - a wrong attachment reported as a success, which is
        worse than a visible failure.
        """
        a = self._download("15550111@s.whatsapp.net", "ABC123", b"first chat")
        b = self._download("120363040000000001@g.us", "ABC123", b"second chat")

        self.assertNotEqual(a.media_path, b.media_path)
        self.assertEqual(Path(a.media_path).read_bytes(), b"first chat")
        self.assertEqual(Path(b.media_path).read_bytes(), b"second chat")

    def test_re_running_a_sync_reuses_the_file_rather_than_duplicating_it(self):
        first = self._download("15550111@s.whatsapp.net", "ABC123", b"payload")
        again = self._download("15550111@s.whatsapp.net", "ABC123", b"payload")
        self.assertEqual(first.media_path, again.media_path)
        self.assertEqual(len(list(self.profile.media_dir.iterdir())), 1)


class TestSessionSendPaths(unittest.TestCase):
    """WhatsAppSession's two send methods, which had no test of any kind.

    That gap let a NameError sit in ``send_text`` - the one method every
    outbound row goes through - while the whole suite stayed green. A test that
    never executes the shipped function proves nothing about it, so these drive
    the real methods and only stub the two things that would otherwise need a
    live Go core: the JID conversion and the client underneath.
    """

    class _Recorder:
        def __init__(self):
            self.calls = []

        def _ok(self, name, *args, **kw):
            self.calls.append((name, args, kw))
            return type("Response", (), {"ID": "MSG1"})()

        def send_message(self, *a, **kw):
            return self._ok("send_message", *a, **kw)

        def send_image(self, *a, **kw):
            return self._ok("send_image", *a, **kw)

        def send_audio(self, *a, **kw):
            return self._ok("send_audio", *a, **kw)

        def send_document(self, *a, **kw):
            return self._ok("send_document", *a, **kw)

    def _session(self):
        from whatsapp_core.client import WhatsAppSession

        session = WhatsAppSession.__new__(WhatsAppSession)
        session._client = self._Recorder()
        session.send_timeout = 10
        session.log = lambda *_a, **_k: None
        session._to_native_jid = lambda value: str(value)
        return session

    @staticmethod
    def _jid():
        return jid.parse_destination("+1 415 555 0100")

    def test_send_text_returns_a_result(self):
        session = self._session()
        result = session.send_text(self._jid(), "hello")
        self.assertTrue(result.success)
        self.assertEqual(result.message_id, "MSG1")
        self.assertEqual(session._client.calls[0][0], "send_message")

    def test_an_image_reports_its_caption_as_delivered(self):
        tmp = Path(tempfile.mkdtemp(prefix="wa-send-"))
        try:
            photo = tmp / "picture.jpg"
            photo.write_bytes(b"jpeg")
            result = self._session().send_file(self._jid(), photo, caption="look")
            self.assertTrue(result.caption_sent)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_voice_note_reports_that_its_caption_was_not_delivered(self):
        """send_audio has no caption parameter; the text has to go separately.

        Without this signal the caption was handed over, dropped, and the row
        still said Success.
        """
        tmp = Path(tempfile.mkdtemp(prefix="wa-send-"))
        try:
            clip = tmp / "note.ogg"
            clip.write_bytes(b"ogg")
            session = self._session()
            with self._pretend_ffmpeg_is_installed():
                result = session.send_file(self._jid(), clip, caption="listen to this")
            # Without FFmpeg the file goes out as a document, which *can* carry
            # a caption - so the branch this guards would never be reached on a
            # build machine that happens not to have it, and the test would
            # quietly pass while proving nothing.
            self.assertEqual(session._client.calls[0][0], "send_audio")
            self.assertFalse(result.caption_sent)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @staticmethod
    @contextlib.contextmanager
    def _pretend_ffmpeg_is_installed():
        from whatsapp_core import client as client_mod

        original = client_mod._ffmpeg_available
        client_mod._ffmpeg_available = lambda: True
        try:
            yield
        finally:
            client_mod._ffmpeg_available = original

    def test_a_voice_note_with_no_caption_has_nothing_outstanding(self):
        tmp = Path(tempfile.mkdtemp(prefix="wa-send-"))
        try:
            clip = tmp / "note.ogg"
            clip.write_bytes(b"ogg")
            result = self._session().send_file(self._jid(), clip)
            self.assertTrue(result.caption_sent)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestSenderSend(unittest.TestCase):
    """Sender.send - the method every outbound row goes through.

    It had no executing coverage at all, which is how a wrong keyword argument
    reached a shipped build twice. The first attempt at a test for this copied
    the branch out of sender.py and ran the copy, so it passed against code
    with the fix deleted. These drive the real method, with only the WhatsApp
    session replaced; reverting any of the behaviour they describe fails them.
    """

    class _Session:
        """Stands in for WhatsAppSession, recording what it was asked to send."""

        def __init__(self, caption_sent=True):
            self._caption_sent = caption_sent
            self.files, self.texts = [], []

        def send_file(self, jid, path, caption=""):
            from whatsapp_core.client import SendResult

            self.files.append((str(jid), str(path), caption))
            return SendResult(chat_id=str(jid), message_id="F1",
                              caption_sent=self._caption_sent)

        def send_text(self, jid, body):
            from whatsapp_core.client import SendResult

            self.texts.append((str(jid), body))
            return SendResult(chat_id=str(jid), message_id="T1")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wa-send-"))
        self.store = store.Store(self.tmp / "archive.sqlite3")

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _sender(self, session):
        from whatsapp_core import sender as sender_mod

        instance = sender_mod.Sender.__new__(sender_mod.Sender)
        instance.settings = config.OutputSettings.from_config({
            "ToSource": "Fixed", "ToFixed": "+14155550100",
            "MessageSource": "Fixed", "MessageFixed": "hello",
        })
        instance.log = lambda _m: None
        instance._store = self.store
        instance._session = session
        instance._name_cache = {}
        instance._verified = {}
        # Not _Throttle(0): that clamps to one message a minute, so the
        # second send of any follow-up case sleeps for 60 seconds.
        instance._throttle = sender_mod._Throttle(60_000)
        instance.sent = 0
        instance.failed = 0
        # _ensure_open would open a real session; the one above is already set.
        instance._ensure_open = lambda: session
        instance._record = lambda *_a, **_k: None
        return instance

    @staticmethod
    def _request(**kw):
        from whatsapp_core.sender import SendRequest

        kw.setdefault("row_index", 1)
        kw.setdefault("to", "+1 415 555 0100")
        return SendRequest(**kw)

    def test_a_plain_message_sends_one_text_and_no_file(self):
        session = self._Session()
        outcome = self._sender(session).send(self._request(body="hello"))
        self.assertTrue(outcome.success)
        self.assertEqual([t for _j, t in session.texts], ["hello"])
        self.assertEqual(session.files, [])

    def test_an_attachment_with_a_caption_that_travels_sends_one_message(self):
        session = self._Session(caption_sent=True)
        outcome = self._sender(session).send(
            self._request(body="look at this", attachment="picture.jpg"))
        self.assertTrue(outcome.success)
        self.assertEqual(session.files[0][2], "look at this")
        self.assertEqual(session.texts, [], "the caption already carried the text")

    def test_an_attachment_that_cannot_carry_a_caption_follows_up_with_text(self):
        """The voice-note case: the text must arrive, one way or another."""
        session = self._Session(caption_sent=False)
        outcome = self._sender(session).send(
            self._request(body="listen to this", attachment="note.ogg"))
        self.assertTrue(outcome.success)
        self.assertEqual([t for _j, t in session.texts], ["listen to this"])

    def test_an_explicit_caption_and_a_body_send_both(self):
        session = self._Session(caption_sent=True)
        self._sender(session).send(self._request(
            body="the long version", attachment="picture.jpg", caption="short"))
        self.assertEqual(session.files[0][2], "short")
        self.assertEqual([t for _j, t in session.texts], ["the long version"])

    def test_an_explicit_caption_on_a_voice_note_sends_both_separately(self):
        # Neither piece of text can ride along, so both follow the file.
        session = self._Session(caption_sent=False)
        self._sender(session).send(self._request(
            body="the long version", attachment="note.ogg", caption="short"))
        self.assertEqual([t for _j, t in session.texts],
                         ["short", "the long version"])

    def test_a_row_with_nothing_to_send_fails_that_row_only(self):
        session = self._Session()
        sender = self._sender(session)
        outcome = sender.send(self._request(body="", attachment=""))
        self.assertFalse(outcome.success)
        self.assertIn("nothing to send", outcome.error)
        self.assertEqual(sender.failed, 1)
        self.assertEqual(session.texts, [])

    def test_an_unreachable_recipient_fails_that_row_only(self):
        session = self._Session()
        sender = self._sender(session)
        sender._verified["14155550100"] = False
        outcome = sender.send(self._request(body="hello"))
        self.assertFalse(outcome.success)
        self.assertEqual(sender.failed, 1)


class TestChatFilterEntries(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wa-filter-"))
        self.store = store.Store(self.tmp / "archive.sqlite3")

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _resolve(self, *entries):
        from whatsapp_core.runner import _resolve_chat_filter

        return _resolve_chat_filter(self.store, list(entries), log=lambda _m: None)

    def test_a_chat_named_like_a_short_number_resolves_by_name(self):
        """Chats are allowed to be called "2024" or "007".

        parse_destination raises for a number that is not a valid phone number,
        and letting that out failed the whole run over one filter entry rather
        than falling through to the name lookup that was sitting right there.
        """
        jid_value = "120363040000000002@g.us"
        self.store.upsert_chats([
            store.ChatRow(chat_id=jid_value, name="2024", is_group=True)])
        self.assertEqual(self._resolve("2024"), [jid_value])

    def test_an_entry_with_an_unknown_server_is_treated_as_a_name(self):
        jid_value = "120363040000000003@g.us"
        self.store.upsert_chats([
            store.ChatRow(chat_id=jid_value, name="team@example.com", is_group=True)])
        self.assertEqual(self._resolve("team@example.com"), [jid_value])

    def test_a_real_phone_number_still_resolves_directly(self):
        self.assertEqual(self._resolve("+1 415 555 0100"),
                         ["14155550100@s.whatsapp.net"])


if __name__ == "__main__":
    # Running this file directly needs src/ - and, for the plugin tests, the
    # SDK - on the path. run_tests does that at import time, so defer to it
    # rather than half-working or silently collecting nothing.
    import run_tests  # noqa: F401
    unittest.main(verbosity=2)
