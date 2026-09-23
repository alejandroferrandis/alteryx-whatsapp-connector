# WhatsApp Connector for Alteryx Designer

Read and send WhatsApp messages from an Alteryx workflow. Two tools, one
install, nothing to configure on a server.

| | |
| --- | --- |
| **Designer** | 2026.1 (Python 3.13) |
| **Platform** | Windows x64 |
| **Install** | one `.yxi`, ~160 MB |
| **External requirements** | none — see [What's bundled](#whats-bundled) |
| **Version** | 1.0.0 |
| **Licence** | MIT |
| **Author** | Alejandro Ferrandis del Valle |

---

## What it does

**WhatsApp Input** connects to a linked WhatsApp account, collects everything
that arrived since the last run, and returns it as a table. It also publishes a
directory of every chat the account can see, which is how you find the chat ids
you need.

**WhatsApp Output** sends a message for each incoming row — to a phone number, a
chat id, or a chat name — with optional file attachments, an outbound rate
limit, and a per-row success/failure result anchor.

Both tools link a device from inside Designer: tick a box, type the phone
number, run the workflow, and type the 8-character code it prints into WhatsApp
on your phone. No terminal, no QR code to photograph, no separate setup utility.

---

## Quick start

1. **Install.** Close Designer, download `WhatsApp_1_0_0.yxi` from the
   [Releases](../../releases) page, double-click it, reopen Designer. The tools appear under **Connectors**.
2. **Link a device.** Drop **WhatsApp Input** on the canvas. In its Connection
   section tick **Link this device**, type the phone number of the WhatsApp
   account (with country code), and run the workflow. The results pane shows a
   code like `A1B2C3D4`. On that phone open
   **WhatsApp → Settings → Linked devices → Link a device → Link with phone
   number instead** and type the code.
3. **Read messages.** Untick **Link this device** and run again. Messages arrive
   on the `Messages` anchor; every known chat, with its id, on `Chats`.
4. **Send.** Drop **WhatsApp Output** after any tool, point **Send to** and
   **Message** at columns of your data, and run.

Longer version: [docs/01-quick-start.md](docs/01-quick-start.md).

---

## What's bundled

Everything the connector needs at runtime ships inside the `.yxi`:

- **The WhatsApp protocol implementation.** [neonize](https://github.com/krypton-byte/neonize)
  and its native Go core (`whatsmeow`), the same multi-device WhatsApp Web
  protocol the well-known Node library Baileys speaks. A 17 MB DLL, included.
- **The Alteryx Python SDK**, pyarrow, pandas, numpy, grpc, protobuf and the
  rest — as wheels, installed into a `site-packages` folder inside each tool.

There is **no** Node.js, Docker image, background service, bridge process, or
`pip install` step. Nothing is fetched at install time or first run. Upstream
neonize would download its native core from GitHub if it were missing; the build
replaces that code path with a clear error, so the connector cannot make an
unexpected outbound request. The build refuses to package a payload it cannot
import, with Designer's own interpreter, offline.

The one thing **not** bundled is **FFmpeg**, and nothing requires it. It is a
separate ~80 MB program whose redistributable builds carry licence obligations
that do not belong inside an Alteryx tool. Without it, sending a video or audio
*file* delivers it as a document — the recipient still gets the whole file, they
tap to download rather than play it inline. Everything else, including receiving
and downloading video and audio, is unaffected. Install FFmpeg and put it on the
`PATH` if you want inline playback.

---

## Documentation

| | |
| --- | --- |
| [Quick start](docs/01-quick-start.md) | Install, link, first workflow |
| [Linking a device](docs/02-linking-a-device.md) | Pairing, profiles, re-linking, multiple accounts |
| [WhatsApp Input](docs/03-input-tool.md) | Every setting, both output anchors, column reference |
| [WhatsApp Output](docs/04-output-tool.md) | Destinations, attachments, rate limits, error handling |
| [How it works](docs/05-architecture.md) | Design decisions and why they were made |
| [Building from source](docs/06-building-from-source.md) | Build, test, release |
| [Troubleshooting](docs/07-troubleshooting.md) | Symptoms, causes, fixes |
| [Limits and legal](docs/08-limits-and-legal.md) | Rate limits, ban risk, WhatsApp's terms, licences |
| [Roadmap](docs/09-roadmap.md) | What is planned next, and what is ruled out |

---

## Repository layout

```
WhatsAppConnector/
├── src/
│   ├── whatsapp_core/      the engine: no Alteryx imports, fully testable
│   │   ├── client.py       the WhatsApp session (the only neonize consumer)
│   │   ├── store.py        local SQLite archive and chat directory
│   │   ├── runner.py       link / sync / read use cases
│   │   ├── sender.py       outbound sending, resolution, rate limiting
│   │   ├── messages.py     WhatsApp protobuf -> flat row
│   │   ├── schema.py       the columns each anchor produces
│   │   ├── config.py       tool settings: parsing, defaults, validation
│   │   ├── profiles.py     where a linked device lives, and its lock
│   │   ├── jid.py          phone / chat id / chat name handling
│   │   └── cli.py          command line: doctor, link, sync, send
│   └── ayx_plugins/        thin Alteryx SDK adapters over the engine
├── ui/                     configuration panels (plain HTML, no build step)
├── configuration/          tool icons (both Config.xml files are generated)
├── docs/                   the documentation set linked above
├── examples/               a ready-to-run workflow
├── tools/                  build_yxi.py, check_panels.py, make_icons.py,
│                           whatsapp.cmd (the support CLI)
├── tests/                  142 tests, runnable with Designer's own Python
└── dist/                   the built .yxi (not in the repo - see Releases)
```

The engine knows nothing about Alteryx, so all of its behaviour can
be tested and debugged from a terminal. See
[docs/05-architecture.md](docs/05-architecture.md).

---

## Running the tests

```bash
"C:\Program Files\Alteryx\bin\Python\python-3.13.11-embed-amd64\python.exe" tests\run_tests.py
```

Using Designer's own interpreter means the tests exercise exactly the Python the
tools will run on. The plugin tests need pyarrow and the SDK; the runner finds
them in a build or an installed copy automatically, and skips those tests if
neither is present.

---

## Building

```bash
python tools\build_yxi.py
```

Needs any Python 3.13 with `pip` (Designer's embedded one has no pip). Produces
`dist/WhatsApp_<version>.yxi`. Intermediates go to `%LOCALAPPDATA%`, never into
the project folder — a build stages ~800 MB and a synced folder would try to
upload all of it.

Details: [docs/06-building-from-source.md](docs/06-building-from-source.md).

---

## Licence

**MIT** — see [LICENSE](LICENSE). Use it, change it, redistribute it.

The bundled `.yxi` also contains third-party components under their own
licences, including a compiled build of **whatsmeow** under **MPL-2.0**.
Those are listed in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md), which
ships inside the package.

Author: Alejandro Ferrandis del Valle

---

## Legal

This product is not affiliated with, endorsed by, or connected to WhatsApp LLC
or Meta Platforms, Inc. "WhatsApp" is their trademark and is used here only to
describe what the connector interoperates with.

It talks to WhatsApp using the unofficial multi-device Web protocol rather than
the WhatsApp Business API. Read [docs/08-limits-and-legal.md](docs/08-limits-and-legal.md)
before deploying it — the ban risk is real and manageable, but you should decide
that knowingly.
