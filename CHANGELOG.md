# Changelog

All notable changes to the WhatsApp Connector for Alteryx Designer,
by Alejandro Ferrandis del Valle.
This project follows [Semantic Versioning](https://semver.org/).

## 1.0.0 — 2026-09-23

First release.

### Added

- **WhatsApp Input** tool. Connects to a linked account, collects everything
  that arrived since the last run into a local SQLite archive, and returns it.
  Two anchors: `Messages` and a `Chats` directory that makes chat ids
  discoverable.
- **WhatsApp Output** tool. Sends one message per incoming row to a phone
  number, chat id or chat name, with optional attachments, an evenly-paced
  outbound rate limit, and a per-row `Results` anchor.
- **In-Designer device linking.** Tick a box, enter a phone number, run the
  workflow, and type the 8-character code into WhatsApp on the phone. QR is
  available as a fallback.
- **Two limits on how far back a sync reaches.** "Only read messages sent
  after this device was linked" makes a fresh profile ignore whatever backlog
  WhatsApp had queued while still delivering everything sent from the moment of
  linking onwards, and "Ignore messages older than N days" keeps a workflow that has been paused
  for a month from importing a month of conversation in one go. Both filter
  ingestion only, so neither can lose already-collected history, and anything
  dropped is counted in the Results pane.
- **Profiles**, so one machine can use several WhatsApp accounts, with a
  cross-process lock that prevents two tools corrupting one linked device.
- **Local archive** with idempotent inserts, a per-message watermark for
  "only what is new", date-range and chat filters, and an audit log of
  everything sent.
- **Command line** (`whatsapp_core.cli`): `doctor`, `status`, `link`, `unlink`,
  `sync`, `read`, `chats`, `send`, `prune`.
- 142 tests runnable on Designer's own interpreter, with no network or WhatsApp
  account required.
- Full documentation under `docs/`.

### Packaging

- Fully self-contained `.yxi` (~160 MB). No Node.js, Docker, background service,
  bridge process or `pip install`. Nothing is fetched at install time or on
  first run.
- neonize's runtime download of its native core from GitHub is replaced with an
  offline stub, so a damaged installation fails with a clear message rather than
  silently reaching out to the internet.
- The build verifies its own output by importing the staged payload with
  Designer's interpreter before packaging, and refuses to package a payload that
  does not import.

### Known limitations

- Video and audio files are sent as documents unless FFmpeg is on the `PATH`.
  FFmpeg is not bundled; see `docs/08-limits-and-legal.md`.
- No delivery or read receipts; no message history from before the device was
  linked; no message editing, deletion, reactions or group administration.
- Windows x64 only.
