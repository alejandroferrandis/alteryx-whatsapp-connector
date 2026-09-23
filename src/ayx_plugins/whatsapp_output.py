"""WhatsApp Output - send a message for every incoming row.

Designer streams data in batches, so the tool keeps one WhatsApp session open
from the first batch until :meth:`on_complete`. Opening a session per batch
would be both slow and a good way to get an account flagged.

Rows are buffered rather than sent as they arrive. That buys two things worth
more than the memory: the recipient check can be made once for the whole table
instead of per row, and the outbound rate limiter can pace the whole run rather
than restarting its clock on every batch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ayx_python_sdk.core import Anchor, PluginV2

from whatsapp_core.config import OutputSettings
from whatsapp_core.errors import WhatsAppError
from whatsapp_core.schema import RESULT_COLUMNS, results_to_columns
from whatsapp_core.sender import SendOutcome, SendRequest, Sender, build_request
from whatsapp_core.version import __version__

from . import _arrow

if TYPE_CHECKING:
    from ayx_python_sdk.providers.amp_provider.amp_provider_v2 import AMPProviderV2
    from pyarrow import Table

ANCHOR_RESULTS = "Results"

#: Guard against a workflow accidentally pointed at a million-row table. At the
#: default 20/minute that is about 3.5 days, so it is certainly a mistake.
MAX_ROWS = 100_000


class WhatsAppOutput(PluginV2):
    """Send WhatsApp messages from the rows of an Alteryx workflow."""

    def __init__(self, provider: "AMPProviderV2") -> None:
        self.provider = provider
        self.settings: OutputSettings | None = None
        self.failed = False
        self.requests: list[SendRequest] = []
        self.outcomes: list[SendOutcome] = []
        self.row_number = 0
        self.truncated = False
        self._warned_missing: set[str] = set()

        self.provider.io.info(f"WhatsApp Output {__version__}")
        self.provider.push_outgoing_metadata(ANCHOR_RESULTS, _arrow.to_schema(RESULT_COLUMNS))

        try:
            self.settings = OutputSettings.from_config(self.provider.tool_config or {})
        except WhatsAppError as exc:
            self.failed = True
            self.provider.io.error(str(exc))

    # -- incoming data ----------------------------------------------------

    def on_record_batch(self, batch: "Table", anchor: Anchor) -> None:
        """Collect rows; nothing is sent until every batch has arrived."""
        if self.failed or self.settings is None:
            return
        if self.settings.connection.link_device:
            return  # A linking run ignores its input entirely.

        self._check_columns(batch)

        rows = batch.to_pylist()
        for values in rows:
            if len(self.requests) >= MAX_ROWS:
                if not self.truncated:
                    self.truncated = True
                    self.provider.io.warn(
                        f"More than {MAX_ROWS:,} rows arrived. Only the first "
                        f"{MAX_ROWS:,} will be sent - at the configured pace the rest "
                        "would take days, which is almost certainly not intended. "
                        "Filter the input, or split the run."
                    )
                break
            self.row_number += 1
            try:
                self.requests.append(build_request(self.settings, self.row_number, values))
            except WhatsAppError as exc:
                self.outcomes.append(
                    SendOutcome(row_index=self.row_number, to="", error=str(exc))
                )

    def _check_columns(self, batch: "Table") -> None:
        """Warn once per missing column, rather than once per row.

        A misspelled column name would otherwise fail every row with an empty
        destination and no clue why.
        """
        present = set(batch.schema.names)
        for wanted in self.settings.needed_fields:
            if wanted not in present and wanted not in self._warned_missing:
                self._warned_missing.add(wanted)
                self.provider.io.warn(
                    f"The incoming data has no column called '{wanted}'. "
                    f"Available columns: {', '.join(sorted(present)) or '(none)'}. "
                    "Re-select the column in the tool's configuration."
                )

    def on_incoming_connection_complete(self, anchor: Anchor) -> None:
        """Nothing to do per connection; sending happens in on_complete."""

    # -- the actual work --------------------------------------------------

    def on_complete(self) -> None:
        """Do the work, then write the result anchor exactly once.

        The single write is why this is shaped as a try/finally rather than
        having each branch write for itself. Writing a table to an anchor
        *appends* to it, so calling ``_write_results`` from both ``_run_send``
        and the end of this method duplicated every result row - one message
        sent, two rows out.
        """
        if self.provider.environment.update_only:
            return

        try:
            if self.failed or self.settings is None:
                return  # The configuration error was already reported.
            if self.settings.connection.link_device:
                self._run_link()
            else:
                self._run_send()
        except WhatsAppError as exc:
            self.provider.io.error(str(exc))
        except Exception as exc:  # noqa: BLE001
            self.provider.io.error(self._crash_message(exc))
        finally:
            self._write_results()

    def _crash_message(self, exc: Exception) -> str:
        """Report an unexpected failure, and save the traceback for support."""
        from whatsapp_core import diagnostics
        from whatsapp_core.profiles import Profile, default_data_root

        directory = default_data_root()
        try:
            if self.settings is not None:
                directory = Profile.open(
                    self.settings.connection.profile,
                    self.settings.connection.resolved_data_dir,
                ).root
        except Exception:  # noqa: BLE001 - we are already failing
            pass

        report = diagnostics.write_report(
            directory, exc, tool="WhatsAppOutput",
            tool_config=self.provider.tool_config or {},
            extra={"rows_buffered": len(self.requests),
                   "rows_attempted": len(self.outcomes)},
        )
        message = f"The WhatsApp Output tool failed unexpectedly: {exc}"
        if report:
            message += (
                f"\nA full report was saved to:\n  {report}\n"
                "Send that file with any bug report - it contains the traceback "
                "and this tool's settings, with personal details redacted."
            )
        return message

    def _run_link(self) -> None:
        from whatsapp_core import runner

        self.provider.io.info(
            "Running in 'Link this device' mode - no messages will be sent. "
            "Untick it once linking is done."
        )
        result = runner.link_device(self.settings.connection, log=self.provider.io.info)
        if result.success:
            self.provider.io.info("Linked. Untick 'Link this device' and run again to send.")
        else:
            self.provider.io.error(result.message)

    def _run_send(self) -> None:
        if not self.requests:
            self.provider.io.info("No rows to send.")
            return

        total = len(self.requests)
        estimate = total * 60.0 / max(self.settings.messages_per_minute, 1)
        self.provider.io.info(
            f"Sending {total} message(s) at {self.settings.messages_per_minute} per "
            f"minute (about {estimate / 60:.1f} minute(s))."
        )

        with Sender(self.settings, log=self.provider.io.info) as sender:
            # Connect explicitly, before any row is attempted. Without this the
            # first send would hit the connection failure, catch it as a per-row
            # error, and repeat it for every remaining row - turning one
            # actionable problem ("this profile is not linked") into a thousand
            # identical failed rows and no error on the tool.
            sender.connect()

            try:
                sender.verify(self.requests)
            except WhatsAppError as exc:
                # Verification is a nicety; never let it stop the send.
                self.provider.io.warn(f"Recipient check skipped: {exc}")

            for index, request in enumerate(self.requests, start=1):
                self.outcomes.append(sender.send(request))
                # Designer's progress bar is the only feedback during a long run.
                self.provider.io.update_progress(index / total)

            self.provider.io.info(sender.summary())
            if sender.failed and not self.settings.fail_on_error:
                self.provider.io.warn(
                    f"{sender.failed} message(s) could not be sent. Their rows are on "
                    "the output anchor with Success = False and the reason in Error."
                )

    # -- anchor helpers ---------------------------------------------------

    def _write_results(self) -> None:
        try:
            # Keep the result rows in input order, whatever order they failed in.
            ordered = sorted(self.outcomes, key=lambda o: o.row_index)
            table = _arrow.to_table(RESULT_COLUMNS, results_to_columns(ordered))
            self.provider.write_to_anchor(ANCHOR_RESULTS, table)
        except Exception as exc:  # noqa: BLE001
            self.provider.io.warn(f"Could not write the results anchor: {exc}")
