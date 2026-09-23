"""Tests for the Alteryx-facing layer, driven by a stand-in provider.

Designer talks to a plugin through one object: the provider. Faking it lets the
whole SDK-facing surface be tested without Designer, a linked WhatsApp account
or a network - which matters, because these are the code paths where a mistake
shows up as a red tool on a customer's canvas rather than as a failing import.

What is asserted here:

* metadata is published for every anchor, including when the configuration is
  invalid, so downstream tools keep their field mappings
* a bad configuration produces an error message and empty, correctly-shaped
  tables rather than an exception
* update-only mode never does any work
* the archive-only path returns real rows, with Alteryx types attached
* the output tool buffers rows, warns once about a missing column, and reports
  per-row failures on its anchor instead of failing the run

These tests are skipped unless pyarrow and the SDK are importable, so the suite
still runs on a machine that only has the engine source.
"""

from __future__ import annotations

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

try:
    import pyarrow as pa
    from ayx_python_sdk.core import Metadata  # noqa: F401

    SDK_AVAILABLE = True
except ImportError:  # pragma: no cover
    SDK_AVAILABLE = False

if SDK_AVAILABLE:
    from ayx_plugins import _arrow
    from ayx_plugins.whatsapp_input import WhatsAppInput
    from ayx_plugins.whatsapp_output import WhatsAppOutput

from whatsapp_core.schema import CHAT_COLUMNS, MESSAGE_COLUMNS, RESULT_COLUMNS
from whatsapp_core.sender import SendOutcome
from whatsapp_core.store import MessageRow, Store


# --------------------------------------------------------------------------
# the stand-in provider
# --------------------------------------------------------------------------


class FakeIO:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.progress: list[float] = []

    def info(self, message: str) -> None:
        self.infos.append(str(message))

    def warn(self, message: str) -> None:
        self.warnings.append(str(message))

    def error(self, message: str) -> None:
        self.errors.append(str(message))

    def update_progress(self, fraction: float) -> None:
        self.progress.append(fraction)

    def translate_msg(self, message: str, *args: object) -> str:
        return message


class FakeEnvironment:
    def __init__(self, update_only: bool = False) -> None:
        self.update_only = update_only
        self.designer_version = "2026.1"


class FakeProvider:
    """Everything the plugins touch on AMPProviderV2, and nothing else."""

    def __init__(self, config: dict, update_only: bool = False) -> None:
        self.tool_config = config
        self.io = FakeIO()
        self.environment = FakeEnvironment(update_only)
        self.metadata: dict[str, object] = {}
        self.written: dict[str, list] = {}

    def push_outgoing_metadata(self, anchor: str, metadata: object) -> None:
        # Do exactly what the real provider does with this argument.
        #
        # AMPProviderV2.push_outgoing_metadata calls
        # ``metadata.serialize().to_pybytes()`` - it wants a pyarrow.Schema,
        # despite "metadata" suggesting the SDK's own Metadata class. An earlier
        # version of this fake just stored whatever it was handed, so passing a
        # Metadata object sailed through every test and then failed in Designer
        # with "'Metadata' object has no attribute 'serialize'".
        #
        # A fake that accepts more than the real thing is worse than no fake.
        metadata.serialize().to_pybytes()
        self.metadata[anchor] = metadata

    def write_to_anchor(self, anchor: str, table: object) -> None:
        self.written.setdefault(anchor, []).append(table)

    # -- helpers for assertions ------------------------------------------

    def table(self, anchor: str):
        tables = self.written.get(anchor, [])
        if not tables:
            raise AssertionError(f"nothing was written to the '{anchor}' anchor")
        return pa.concat_tables(tables)

    @property
    def all_messages(self) -> str:
        return "\n".join(self.io.infos + self.io.warnings + self.io.errors)


def anchor(name: str, columns) -> "pa.Table":
    return _arrow.empty_table(columns)


@unittest.skipUnless(SDK_AVAILABLE, "pyarrow / ayx_python_sdk not importable")
class TestArrowMapping(unittest.TestCase):
    def test_every_anchor_builds_with_alteryx_metadata(self):
        for columns in (MESSAGE_COLUMNS, CHAT_COLUMNS, RESULT_COLUMNS):
            table = _arrow.empty_table(columns)
            self.assertEqual(list(table.schema.names), [c.name for c in columns])
            for field, column in zip(table.schema, columns):
                # Designer reads the Alteryx type from this metadata, never from
                # the Arrow type, so its absence is a silent corruption.
                self.assertIn(b"ayx.type", field.metadata or {})
                self.assertIn(b"ayx.size", field.metadata or {})

    def test_datetime_columns_carry_no_subsecond_precision(self):
        """An Alteryx DateTime holds whole seconds.

        Sending ``date64`` (milliseconds) made the engine emit one
        "has too many digits after the decimal and was truncated" warning per
        value, and a workflow hit its field-conversion error limit on the first
        run. ``timestamp('s')`` has nothing to truncate.
        """
        table = _arrow.empty_table(MESSAGE_COLUMNS)
        self.assertEqual(table.schema.field("Timestamp").type, pa.timestamp("s"))

    def test_datetime_fields_declare_the_alteryx_text_width(self):
        # "yyyy-mm-dd hh:mm:ss" is 19 characters; leaving size at 0 makes the
        # engine guess at the column width.
        schema = _arrow.to_schema(MESSAGE_COLUMNS)
        self.assertEqual(schema.field("Timestamp").metadata[b"ayx.size"], b"19")

    def test_every_field_declares_the_alteryx_type(self):
        schema = _arrow.to_schema(MESSAGE_COLUMNS)
        for field in schema:
            self.assertIn(b"ayx.type", field.metadata or {}, field.name)

    def test_values_round_trip(self):
        rows = [
            MessageRow(
                message_id="A", chat_id="1@s.whatsapp.net", chat_name="Ana",
                body="hola", timestamp=datetime(2026, 9, 1, 10, 30, tzinfo=timezone.utc),
            )
        ]
        from whatsapp_core.schema import messages_to_columns

        table = _arrow.to_table(MESSAGE_COLUMNS, messages_to_columns(rows))
        self.assertEqual(table.num_rows, 1)
        self.assertEqual(table.column("Body")[0].as_py(), "hola")

    def test_datetime_keeps_the_time_of_day(self):
        """The whole point of DateTime: the clock time must survive.

        An earlier encoding (``date64``) round-tripped through Python as a bare
        ``date``, losing the time in any readback. ``timestamp('s')`` keeps it,
        to the second.
        """
        from whatsapp_core.schema import messages_to_columns

        when = datetime(2026, 9, 1, 10, 30, 45, tzinfo=timezone.utc)
        table = _arrow.to_table(
            MESSAGE_COLUMNS,
            messages_to_columns([MessageRow(message_id="A", chat_id="1@s.whatsapp.net",
                                            timestamp=when)]),
        )
        value = table.column("Timestamp")[0].as_py()
        self.assertEqual(value.hour, 10)
        self.assertEqual(value.minute, 30)
        self.assertEqual(value.second, 45)
        self.assertEqual(value.microsecond, 0, "no sub-second precision may survive")

    def test_missing_datetimes_stay_null(self):
        """A chat that has never produced a message has no LastMessage.

        Turning that into 1970-01-01 puts a plausible-looking but fabricated
        date on over a thousand rows.
        """
        from whatsapp_core.schema import chats_to_columns
        from whatsapp_core.store import ChatRow

        table = _arrow.to_table(
            CHAT_COLUMNS,
            chats_to_columns([ChatRow(chat_id="1@g.us", name="Team", is_group=True)]),
        )
        self.assertIsNone(table.column("LastMessage")[0].as_py())


@unittest.skipUnless(SDK_AVAILABLE, "pyarrow / ayx_python_sdk not importable")
class TestInputPlugin(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wa-plugin-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _config(self, **overrides) -> dict:
        config = {
            "Profile": "testing",
            "DataDir": str(self.tmp),
            "Mode": "ArchiveOnly",
            "OnlyNew": "False",
        }
        config.update(overrides)
        return config

    def _seed_archive(self) -> None:
        from whatsapp_core.profiles import Profile

        profile = Profile.open("testing", self.tmp)
        with Store(profile.archive_db) as store:
            store.add_messages([
                MessageRow(
                    message_id="m1", chat_id="15550100@s.whatsapp.net",
                    sender_name="Ana", body="hola",
                    timestamp=datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc),
                ),
                MessageRow(
                    message_id="m2", chat_id="120363@g.us", is_group=True,
                    sender_name="Bob", body="team update",
                    timestamp=datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc),
                ),
            ])

    def test_publishes_metadata_for_both_anchors(self):
        provider = FakeProvider(self._config())
        WhatsAppInput(provider)
        self.assertIn("Messages", provider.metadata)
        self.assertIn("Chats", provider.metadata)
        # It must be a pyarrow Schema carrying the Alteryx field metadata -
        # that is what Designer deserialises on the other end.
        schema = provider.metadata["Messages"]
        self.assertIsInstance(schema, pa.Schema)
        self.assertEqual(list(schema.names), [c.name for c in MESSAGE_COLUMNS])
        self.assertIn(b"ayx.type", schema.field("Body").metadata or {})

    def test_invalid_configuration_errors_and_still_emits_shaped_tables(self):
        provider = FakeProvider(
            self._config(IncludeGroups="False", IncludeDirect="False")
        )
        plugin = WhatsAppInput(provider)
        plugin.on_complete()

        self.assertTrue(provider.io.errors, "an invalid configuration must report an error")
        self.assertIn("never return a row", provider.all_messages)
        # Downstream tools still need the columns.
        self.assertEqual(provider.table("Messages").num_rows, 0)
        self.assertEqual(
            list(provider.table("Messages").schema.names),
            [c.name for c in MESSAGE_COLUMNS],
        )

    def test_update_only_mode_does_no_work(self):
        provider = FakeProvider(self._config(), update_only=True)
        plugin = WhatsAppInput(provider)
        plugin.on_complete()
        self.assertIn("Messages", provider.metadata)
        self.assertNotIn("Messages", provider.written)

    def test_archive_only_returns_rows_without_connecting(self):
        self._seed_archive()
        provider = FakeProvider(self._config())
        plugin = WhatsAppInput(provider)
        plugin.on_complete()

        self.assertFalse(provider.io.errors, provider.all_messages)
        table = provider.table("Messages")
        self.assertEqual(table.num_rows, 2)
        self.assertEqual(table.column("Body").to_pylist(), ["hola", "team update"])
        self.assertEqual(table.column("IsGroup").to_pylist(), [False, True])

    def test_chat_filter_applies(self):
        self._seed_archive()
        provider = FakeProvider(self._config(Chats="120363@g.us"))
        WhatsAppInput(provider).on_complete()
        table = provider.table("Messages")
        self.assertEqual(table.column("MessageId").to_pylist(), ["m2"])

    def test_only_new_watermark_moves_after_emitting(self):
        self._seed_archive()
        first = FakeProvider(self._config(OnlyNew="True"))
        WhatsAppInput(first).on_complete()
        self.assertEqual(first.table("Messages").num_rows, 2)

        second = FakeProvider(self._config(OnlyNew="True"))
        WhatsAppInput(second).on_complete()
        self.assertEqual(second.table("Messages").num_rows, 0)
        # The empty result must name the setting responsible instead of guessing.
        self.assertIn(
            "already been returned by an earlier run", second.all_messages
        )

    def test_empty_result_blames_the_right_filter(self):
        """Each filter, when it is the cause, must be the one named."""
        self._seed_archive()

        # A date range that excludes everything. UseDateRange must be on: the
        # bounds are ignored without it.
        provider = FakeProvider(
            self._config(UseDateRange="True", DateFrom="2020-01-01", DateTo="2020-01-02")
        )
        WhatsAppInput(provider).on_complete()
        self.assertIn("date range excludes", provider.all_messages)

        # And without the switch, the same bounds filter nothing at all.
        provider = FakeProvider(self._config(DateFrom="2020-01-01", DateTo="2020-01-02"))
        WhatsAppInput(provider).on_complete()
        self.assertEqual(provider.table("Messages").num_rows, 2)

        # A chat type switched off, when every message is in the other kind.
        provider = FakeProvider(self._config(IncludeDirect="False"))
        WhatsAppInput(provider).on_complete()
        self.assertEqual(provider.table("Messages").num_rows, 1)  # the group one

    def test_settings_summary_is_logged(self):
        provider = FakeProvider(self._config())
        WhatsAppInput(provider).on_complete()
        self.assertIn("Settings: profile \"testing\"", provider.all_messages)

    def test_group_filter_excludes_direct_chats(self):
        self._seed_archive()
        provider = FakeProvider(self._config(IncludeDirect="False"))
        WhatsAppInput(provider).on_complete()
        self.assertEqual(provider.table("Messages").column("MessageId").to_pylist(), ["m2"])

    def test_unlinked_profile_reports_how_to_link(self):
        provider = FakeProvider(self._config(Mode="Sync"))
        WhatsAppInput(provider).on_complete()
        self.assertTrue(provider.io.errors)
        self.assertIn("not connected to WhatsApp yet", provider.all_messages)
        self.assertIn("Link this device", provider.all_messages)


@unittest.skipUnless(SDK_AVAILABLE, "pyarrow / ayx_python_sdk not importable")
class TestOutputPlugin(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wa-out-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _config(self, **overrides) -> dict:
        config = {
            "Profile": "testing",
            "DataDir": str(self.tmp),
            "ToSource": "Field",
            "ToField": "Phone",
            "MessageSource": "Field",
            "MessageField": "Text",
        }
        config.update(overrides)
        return config

    @staticmethod
    def _batch(rows: list[dict]) -> "pa.Table":
        columns = {key: [row.get(key) for row in rows] for key in rows[0]}
        return pa.table(columns)

    def test_publishes_results_metadata(self):
        provider = FakeProvider(self._config())
        WhatsAppOutput(provider)
        self.assertIn("Results", provider.metadata)

    def test_unmapped_column_is_a_configuration_error(self):
        provider = FakeProvider(self._config(ToField=""))
        WhatsAppOutput(provider).on_complete()
        self.assertTrue(provider.io.errors)
        self.assertIn("no column is selected", provider.all_messages)

    def test_missing_column_warns_once_and_names_what_is_there(self):
        provider = FakeProvider(self._config())
        plugin = WhatsAppOutput(provider)
        batch = self._batch([{"Wrong": "x", "Text": "hi"}, {"Wrong": "y", "Text": "ho"}])
        plugin.on_record_batch(batch, anchor=None)
        plugin.on_record_batch(batch, anchor=None)

        warnings = [w for w in provider.io.warnings if "no column called 'Phone'" in w]
        self.assertEqual(len(warnings), 1, "the warning must not repeat per row or batch")
        self.assertIn("Wrong", warnings[0])

    def test_rows_are_buffered_not_sent_during_batches(self):
        provider = FakeProvider(self._config())
        plugin = WhatsAppOutput(provider)
        plugin.on_record_batch(
            self._batch([{"Phone": "+15550100", "Text": "hi"}]), anchor=None
        )
        self.assertEqual(len(plugin.requests), 1)
        # Nothing has been written yet: sending happens in on_complete.
        self.assertNotIn("Results", provider.written)

    def test_unlinked_profile_fails_cleanly_and_still_writes_results(self):
        provider = FakeProvider(self._config())
        plugin = WhatsAppOutput(provider)
        plugin.on_record_batch(
            self._batch([{"Phone": "+15550100", "Text": "hi"}]), anchor=None
        )
        plugin.on_complete()

        self.assertTrue(provider.io.errors)
        self.assertIn("not connected to WhatsApp yet", provider.all_messages)
        table = provider.table("Results")
        self.assertEqual(list(table.schema.names), [c.name for c in RESULT_COLUMNS])

    def test_results_anchor_is_written_exactly_once(self):
        """One input row must produce one result row.

        Writing a table to an anchor *appends*, so calling the write helper
        from both ``_run_send`` and ``on_complete`` silently doubled every
        result. The row count alone did not catch it here because the test
        concatenated the writes - so this asserts the number of *writes*.
        """
        # Link mode is excluded here: it opens a real WhatsApp
        # connection and waits to be paired, which has no place in a unit test.
        for label, config in (
            ("unlinked profile", self._config()),
            ("no rows", self._config()),
            ("bad configuration", self._config(ToField="")),
        ):
            provider = FakeProvider(config)
            plugin = WhatsAppOutput(provider)
            if label != "no rows":
                plugin.on_record_batch(
                    self._batch([{"Phone": "+15550100", "Text": "hi"}]), anchor=None
                )
            plugin.on_complete()
            self.assertEqual(
                len(provider.written.get("Results", [])), 1,
                f"{label}: the Results anchor must be written exactly once",
            )

    def test_outcomes_reach_the_anchor_once_and_in_input_order(self):
        """Each recorded outcome becomes exactly one result row.

        Sending for real needs a linked account, so the outcomes are injected
        directly: this is a test of the write path, which is where the
        duplication bug lived; the sending path is covered elsewhere.
        """
        provider = FakeProvider(self._config())
        plugin = WhatsAppOutput(provider)
        plugin.outcomes = [
            SendOutcome(row_index=3, to="c", success=True, message_id="m3"),
            SendOutcome(row_index=1, to="a", success=True, message_id="m1"),
            SendOutcome(row_index=2, to="b", success=False, error="nope"),
        ]
        plugin.on_complete()

        self.assertEqual(len(provider.written["Results"]), 1, "written exactly once")
        table = provider.table("Results")
        self.assertEqual(table.num_rows, 3)
        # Sorted back into input order, whatever order they failed in.
        self.assertEqual(table.column("RowNumber").to_pylist(), [1, 2, 3])
        self.assertEqual(table.column("Success").to_pylist(), [True, False, True])

    def test_connection_failure_is_one_error_not_one_row_each(self):
        """A dead profile is a run-level failure that must not become N row failures.

        Reporting "not linked" once on the tool is far more actionable than
        stamping the same message onto every row of a thousand-row table.
        """
        provider = FakeProvider(self._config())
        plugin = WhatsAppOutput(provider)
        plugin.on_record_batch(
            self._batch([{"Phone": "+1555012{}".format(i), "Text": "hi"}
                         for i in range(5)]),
            anchor=None,
        )
        plugin.on_complete()
        self.assertEqual(len(provider.io.errors), 1)
        self.assertIn("not connected to WhatsApp yet", provider.all_messages)
        self.assertEqual(provider.table("Results").num_rows, 0)

    def test_empty_input_does_not_connect(self):
        provider = FakeProvider(self._config())
        plugin = WhatsAppOutput(provider)
        plugin.on_complete()
        self.assertIn("No rows to send", provider.all_messages)
        self.assertEqual(provider.table("Results").num_rows, 0)

    def test_link_mode_ignores_incoming_data(self):
        provider = FakeProvider(self._config(LinkDevice="True", LinkPhone="+15550100"))
        plugin = WhatsAppOutput(provider)
        plugin.on_record_batch(
            self._batch([{"Phone": "+15550100", "Text": "hi"}]), anchor=None
        )
        self.assertEqual(plugin.requests, [])

    def test_fixed_values_need_no_columns(self):
        provider = FakeProvider(
            self._config(
                ToSource="Fixed", ToFixed="+15550100", ToField="",
                MessageSource="Fixed", MessageFixed="hello", MessageField="",
            )
        )
        plugin = WhatsAppOutput(provider)
        self.assertFalse(provider.io.errors, provider.all_messages)
        plugin.on_record_batch(self._batch([{"Anything": 1}]), anchor=None)
        self.assertEqual(plugin.requests[0].to, "+15550100")
        self.assertEqual(plugin.requests[0].body, "hello")



@unittest.skipUnless(SDK_AVAILABLE, "pyarrow / ayx_python_sdk not importable")
class TestIngestFloorIsActuallyApplied(unittest.TestCase):
    """The floor has to bite inside the drain, not merely compute correctly.

    resolve_ingest_floor is unit-tested elsewhere. What this covers is the
    wiring: that _sync consults it for every message and discards the old ones
    before they reach the archive. A correct function nobody calls is worth
    nothing, and that failure mode is invisible to a test of the function.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wa-drain-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _event(message_id: str, when: datetime):
        """A protobuf message event, as neonize would deliver it."""
        from neonize.proto import Neonize_pb2 as pb

        ev = pb.Message()
        ev.Info.ID = message_id
        ev.Info.Timestamp = int(when.timestamp())
        ev.Info.Pushname = "Tester"
        ev.Info.MessageSource.Chat.User = "15550100"
        ev.Info.MessageSource.Chat.Server = "s.whatsapp.net"
        ev.Message.conversation = f"body {message_id}"
        return ev

    def _run_sync(self, events, **config_overrides):
        """Drive runner._sync with a fake session that replays `events`."""
        from whatsapp_core import runner
        from whatsapp_core.profiles import Profile
        from whatsapp_core.store import Store

        class FakeSession:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def wait_until_ready(self):
                pass

            def own_jid(self):
                return "15550100@s.whatsapp.net"

            def drain(self, handler, **kw):
                for ev in events:
                    handler(ev)
                return len(events)

            def chat_directory(self):
                return []

        config = {"Profile": "drain", "DataDir": str(self.tmp)}
        config.update(config_overrides)
        from whatsapp_core.config import InputSettings

        settings = InputSettings.from_config(config)
        profile = Profile.open("drain", str(self.tmp))

        original = runner.WhatsAppSession
        runner.WhatsAppSession = FakeSession
        try:
            with Store(profile.archive_db) as store:
                stats = runner._sync(profile, store, settings, log=lambda m: None)
                rows = store.query_messages(include_own=True, only_new=False)
        finally:
            runner.WhatsAppSession = original
        return stats, rows

    def test_messages_older_than_the_window_are_dropped_and_counted(self):
        now = datetime.now(timezone.utc)
        events = [
            self._event("recent", now - timedelta(hours=1)),
            self._event("ancient", now - timedelta(days=60)),
        ]
        stats, rows = self._run_sync(
            events, StartFromFirstRun="False", IgnoreOlderThanDays="7"
        )
        self.assertEqual([r.message_id for r in rows], ["recent"])
        self.assertEqual(stats.too_old, 1)
        self.assertEqual(stats.archived, 1)

    def test_nothing_is_dropped_when_both_limits_are_off(self):
        now = datetime.now(timezone.utc)
        events = [
            self._event("recent", now - timedelta(hours=1)),
            self._event("ancient", now - timedelta(days=60)),
        ]
        stats, rows = self._run_sync(
            events, StartFromFirstRun="False", IgnoreOlderThanDays="0"
        )
        self.assertEqual(sorted(r.message_id for r in rows), ["ancient", "recent"])
        self.assertEqual(stats.too_old, 0)

    def test_first_run_excludes_preexisting_backlog(self):
        now = datetime.now(timezone.utc)
        events = [
            self._event("backlog", now - timedelta(minutes=30)),
            self._event("after", now + timedelta(seconds=30)),
        ]
        stats, rows = self._run_sync(
            events, StartFromFirstRun="True", IgnoreOlderThanDays="0"
        )
        self.assertEqual([r.message_id for r in rows], ["after"])
        self.assertEqual(stats.too_old, 1)

    def test_messages_survive_a_crash_partway_through_the_drain(self):
        """The docs promise a failed run costs nothing. Make that true.

        whatsmeow acknowledges messages to WhatsApp as it delivers them, so
        anything still only in memory when something throws is gone for good.
        Buffering the whole drain before the first write made the promise false.
        """
        from whatsapp_core import runner
        from whatsapp_core.config import InputSettings
        from whatsapp_core.profiles import Profile
        from whatsapp_core.store import Store

        now = datetime.now(timezone.utc)
        events = [self._event(f"m{i}", now - timedelta(minutes=i)) for i in range(60)]

        class ExplodingSession:
            def __init__(self, *a, **kw): pass
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def wait_until_ready(self): pass
            def own_jid(self): return "15550100@s.whatsapp.net"
            def chat_directory(self): return []

            def drain(self, handler, **kw):
                for index, ev in enumerate(events):
                    if index == 55:
                        raise RuntimeError("connection dropped mid-drain")
                    handler(ev)

        settings = InputSettings.from_config({
            "Profile": "crash", "DataDir": str(self.tmp),
            "StartFromFirstRun": "False", "IgnoreOlderThanDays": "0",
        })
        profile = Profile.open("crash", str(self.tmp))

        original = runner.WhatsAppSession
        runner.WhatsAppSession = ExplodingSession
        try:
            with Store(profile.archive_db) as store:
                with self.assertRaises(RuntimeError):
                    runner._sync(profile, store, settings, log=lambda m: None)
        finally:
            runner.WhatsAppSession = original

        # Whatever had been flushed before the failure must still be there.
        with Store(profile.archive_db) as store:
            kept = store.query_messages(include_own=True, only_new=False)
        self.assertGreaterEqual(
            len(kept), 50,
            "messages drained before the failure must already be durable",
        )


if __name__ == "__main__":
    # See the note in test_core.py: run_tests sets up sys.path on import.
    import run_tests  # noqa: F401
    unittest.main(verbosity=2)
