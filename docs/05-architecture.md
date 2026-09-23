# How it works

The design decisions behind the connector, and why each was made. Read this
before changing anything structural.

---

## Layers

```
  Designer
     │  gRPC, Arrow record batches
     ▼
  ayx_plugins/            thin adapters. Translate provider <-> engine.
     whatsapp_input.py
     whatsapp_output.py
     _arrow.py            logical schema -> pyarrow + Alteryx field metadata
     │
     ▼
  whatsapp_core/          the engine. No Alteryx imports anywhere.
     runner.py            use cases: link, sync, read
     sender.py            use case: send, with resolution and pacing
     store.py             local SQLite archive and chat directory
     client.py            the WhatsApp session  <- the ONLY neonize consumer
     messages.py          WhatsApp protobuf -> flat row
     schema.py            the columns each anchor produces
     config.py            settings: parsing, defaults, validation
     profiles.py          where a linked device lives, and its lock
     jid.py               phone / chat id / chat name
     cli.py               terminal access to all of the above
     │
     ▼
  neonize -> whatsmeow (Go, in a bundled DLL) -> WhatsApp servers
```

**The engine imports nothing from Alteryx.** That single rule pays for itself
repeatedly: the whole of the connector's behaviour can be exercised from a
terminal, unit-tested without Designer, and debugged with a normal traceback.
The plugins are left with almost no logic — read the config, call the engine,
write an anchor, report what happened.

**Only `client.py` imports neonize**, and it does so lazily. A run that just
re-reads the archive, or that fails configuration validation, never loads the
17 MB native library.

---

## Why an embedded library rather than a bridge

The obvious way to speak WhatsApp is Baileys, a Node library, behind a small
HTTP service. That is how most WhatsApp automation is built.

It was rejected because it fails the product's core requirement. A bridge means
the customer installs Node.js, runs a service, keeps it alive, and configures a
port. That is three support surfaces and a deployment document.

[neonize](https://github.com/krypton-byte/neonize) is Python bindings over
[whatsmeow](https://github.com/tulir/whatsmeow), the Go implementation of the
same multi-device protocol Baileys speaks. It ships as a wheel containing a
single compiled DLL. That makes it something an Alteryx tool can simply
*contain* — which is the whole difference between a product and an integration
project.

Verified before committing to it: neonize loads and runs on Designer 2026.1's
exact embedded interpreter (CPython 3.13.11, win_amd64).

### Nothing is fetched at runtime

Upstream neonize downloads its native core from GitHub if the bundled copy is
missing or reports an unexpected version. The build replaces that module with
one that raises a clear "reinstall the connector" error instead. A damaged
install therefore fails loudly rather than quietly repairing itself over the
internet, and the connector makes no outbound request other than to WhatsApp.

The build asserts this: it refuses to package a payload whose downloader is not
the offline stub, or whose DLL is absent.

---

## Why a local archive

A WhatsApp link is a *stream*, not a queryable history. A linked device receives
what it missed on reconnect, and then WhatsApp forgets about it.

Piping that stream straight to an output anchor produces a tool that is:

- **unrepeatable** — re-running yields nothing,
- **lossy** — a failure downstream loses the batch,
- **unfilterable** — "last Tuesday" is not a question the stream can answer.

So the Input tool separates **sync** (drain the stream into SQLite, exactly once
per message) from **emit** (query SQLite onto the anchor). Everything good
follows: reproducible re-runs, date filters, a per-message watermark, and a
crash that costs nothing because the messages were already durable.

Design details that matter:

- **Inserts are idempotent** on `(chat_id, message_id)`. WhatsApp re-delivers
  after a reconnect; without this, every sync would duplicate the tail of
  history. Take it out and the tool breaks.
- **The watermark is per message.** A scalar timestamp fails here: messages arrive out of
  order, and anything earlier than the mark would be skipped.
- **The watermark moves only after the rows are written to the anchor.** A
  workflow that dies mid-write re-reads rather than loses.
- **WAL mode**, so a long sync never blocks a reader and a hard kill of Designer
  cannot corrupt the file.
- **Chat names are resolved at read time** and never stored per message, so renaming
  a group applies to history too.

---

## Why the sync stops when it does

Collection ends at the first of:

1. WhatsApp's **`OfflineSyncCompleted`** event *and* an empty queue — the
   authoritative "you are up to date" signal;
2. a short **quiet period** (default 8s) — the fallback, because the completion
   event is not guaranteed;
3. a **hard ceiling** (default 120s) — so a very busy account cannot stall a
   workflow.

Media is downloaded *after* the drain, outside the message handler. A
download takes seconds, and stalling the handler would stop the idle timer from
ever expiring on a chat full of photos.

---

## Threading

`neonize`'s `connect()` **blocks until the session is torn down** — it is an
event loop, not a handshake. Everything else in `client.py` follows from that:
the session runs `connect()` on a worker thread and the calling thread waits on
`threading.Event` objects that the callbacks set.

Two traps worth knowing:

- **Handlers are registered as closures.** neonize stores
  bound methods as *weak references*, so a handler written as
  `self._on_message` can be garbage-collected mid-session and silently stop
  firing.
- **Pairing must happen after the socket is up.** whatsmeow can only issue a
  link code once connected but not logged in, which is exactly what the QR
  callback signals. The session waits for that before calling `PairPhone`.

---

## Concurrency

whatsmeow's device store is single-writer. Two processes on one linked device
corrupt it, and WhatsApp responds by unlinking the device. Designer happily runs
tools in parallel, so a **cross-process lock per profile** is not optional.

The lock is a file containing the owning process's identity. A lock whose owner
is no longer running is reclaimed automatically, so a crashed workflow never
wedges a profile permanently — but a lock left by *another machine* is never
reclaimed, since a PID from another host tells us nothing.

---

## Chat ids, the usability problem

Chat ids are the single biggest trap in any WhatsApp integration: nobody knows
their family group is `120363000000000000@g.us`.

The answer here is three-part:

1. **The Input tool publishes a chat directory** on a second anchor. Run it
   once, read the ids off the canvas. This is the Alteryx-native solution — the
   configuration panel cannot query Python, but a workflow can.
2. **Destinations accept anything reasonable**: a full JID, a phone number in
   any human format, or a chat name resolved against that directory.
3. **Ambiguity is an error, never a guess.** Two chats named "Team" produce a
   failed row listing both ids.

Device and agent suffixes (`15550100:7@s.whatsapp.net`) are stripped when
parsing, because replying to a raw sender id would otherwise fail.

---

## Configuration

Alteryx hands a plugin its configuration as a flat `dict[str, str]`. Everything
arrives as a string and anything the user never touched is absent.

Rather than scatter `tool_config.get("Foo", "false") == "true"` through the
plugins, every setting is declared once in `config.py` with its default, type
and validation. The plugins read typed attributes; the settings can be
exercised from tests and the CLI with no Designer present.

Validation is opinionated:

- **Checkboxes fall back** to their default on an unrecognised value — a
  malformed checkbox should not stop a workflow.
- **Numbers clamp** into range rather than raising — the GUI already constrains
  them, so out-of-range means a hand-edited workflow, and honouring the nearest
  legal value is friendlier than failing.
- **Contradictions are hard errors.** Excluding both group and direct chats
  could never return a row, so it is rejected with an explanation rather than
  silently returning nothing.

Every error message states **what went wrong and what to do about it**. They are
user-facing copy; developer diagnostics go elsewhere.

---

## The configuration panels

Plain HTML on Designer's built-in **HTML GUI SDK v2**, which ships inside
Designer at `RuntimeData/HtmlAssets/Shared/2/lib/includes.html`.

No React, no npm, no bundler. Each panel is a single readable file with a table
of settings at the bottom, and there is nothing to rebuild when Designer updates
its themes. The alternative — Alteryx's React UI SDK, which their own connectors
use — would add a Node toolchain to a product whose selling point is that it
needs none.

The name passed to `AlteryxDataItems.X('Foo')` becomes the XML element the
Python side reads as `tool_config['Foo']`. That pairing is the entire contract
between a panel and `config.py`.

---

## Arrow and Alteryx types

Designer does not read Arrow types directly. Each field must carry `ayx.*`
metadata naming the real Alteryx type, or a text column arrives at the wrong
width and a DateTime arrives as a number. `_arrow.py` builds that metadata with
the SDK's own `Field` class rather than hand-rolling the keys.

One sharp edge, pinned by a test: **DateTime is sent as Arrow
`timestamp('s')`**, not the `date64` the SDK's own `Field` class maps it to.
An Alteryx DateTime holds whole seconds, so `date64`'s milliseconds made the
engine emit *"has too many digits after the decimal and was truncated"* once
per value — ten of those and a workflow hits its field-conversion error limit.
Seconds have nothing to truncate. The `ayx.type` metadata still declares
`FieldType.datetime`, which is what Designer types the column from.

---

## Packaging

The `.yxi` is built by `tools/build_yxi.py` rather than `ayx_plugin_cli`, which
targets Python 3.8 and a Miniconda workspace — two major versions behind the
interpreter Designer 2026.1 runs. The script produces the same *output*, a
layout copied from a known-good Alteryx-published connector for 2026.1.

Each tool carries its own `site-packages`. That is what Alteryx's own connectors
do, and it is worth the disk: a shared folder would have to be referenced by
absolute path from a static manifest, which breaks as soon as a customer
installs somewhere unexpected.

**Pruning is the risky step**, so the build verifies its own output: it imports
the staged payload in a fresh interpreter — Designer's own, when present —
loading neonize, the SDK's 48 generated protobuf modules, both plugin classes,
and building every anchor's table. A payload that does not import is not
packaged. This caught two real defects during development:

- a prune rule that removed `phonenumbers/shortdata`, which the package imports
  eagerly;
- a **protobuf conflict**: the Alteryx SDK pins `protobuf==6.33.5`, but
  neonize's generated code declares gencode 7.34.1 and refuses to load on an
  older runtime. pip honours the SDK's pin, so a default resolve produces a
  package where `import neonize` fails. The fix is to install `protobuf==7.34.1`
  last: protobuf runtimes support gencode from their own major version and the
  one before, so 7.x serves both the SDK's 6.x gencode and neonize's 7.x.

Intermediates are staged outside the project directory. A build writes ~800 MB,
and doing that inside a synced folder makes the sync client upload every
intermediate file and hold locks while it does — which broke a build during
development.
