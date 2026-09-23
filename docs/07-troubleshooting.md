# Troubleshooting

Start here:

```bash
"%APPDATA%\Alteryx\Tools\WhatsAppInput_1_0_0\whatsapp.cmd" doctor
```

`whatsapp.cmd` is installed alongside the Input tool, in the folder shown
above. It locates Designer's interpreter and
the installed tool's bundled libraries by itself — there is no `python` on a
normal machine that can do this, because Designer's interpreter is embedded,
carries no site-packages of its own and ignores `PYTHONPATH`.

`doctor` reports the version, the interpreter, whether the native library loads,
whether FFmpeg is present, and every profile with its link state. It answers
most questions in one step and is the right thing to attach to a bug report.

---

## Installation

### The tools do not appear after installing

- **Designer was open during the install.** Close it completely — check Task
  Manager for `AlteryxGui.exe` — and reinstall.
- **Look under the right category.** Both tools are in **Connectors**. Searching
  the tool palette for `whatsapp` also finds them.
- **Check they were actually written:**
  `%APPDATA%\Alteryx\Tools\WhatsAppInput_1_0_0` should exist and contain
  `main.pyz`, `manifest.json` and a `site-packages` folder.
- **An all-users install** lands in `%ProgramData%\Alteryx\Tools` instead.

### The tool shows a red X immediately

Open the Results pane and read the message — configuration errors are reported
in full, with the fix. If the error mentions an import or a missing module, the
installation is damaged: reinstall the `.yxi`.

### "The bundled component 'neonize' could not be loaded"

The native library is missing or blocked. Almost always antivirus: the
connector ships a 17 MB Go DLL, which some products quarantine on sight.

- Restore `neonize-windows-amd64.dll` from quarantine, or reinstall the `.yxi`.
- Allow-list `%APPDATA%\Alteryx\Tools`.

The connector never downloads anything, so it cannot repair itself — this
failure is deliberate rather than silently fetching a replacement from the
internet.

---

## Linking

### The link code never appears

- Check internet access. WhatsApp Web needs a WebSocket to `*.whatsapp.net` on
  port 443. Corporate proxies that intercept TLS often block it — set
  **Advanced → Proxy** if you have one.
- Raise **Connection timeout**.
- Make sure **Link this device** is ticked and you are reading the **Results**
  pane - the Messages pane will not show it.

### The code is rejected or expires

Codes last about a minute. Run the workflow again for a fresh one.

If it is consistently rejected, check the number: it must be the account's own
number, in full international format. The tool accepts any punctuation but
cannot know the country code if you omit it.

### "Profile is already linked" but nothing works

The tool found a session file and assumed the job was done. Tick **Replace the
existing link** as well, then run.

### WhatsApp keeps unlinking the device

- **The same profile is open twice.** Two live copies of one device get both
  unlinked. Check that Input and Output are not running concurrently on one
  profile, and that the profile folder is not synced to another machine.
- **The profile folder is in OneDrive or on a roaming profile.** Move it. The
  default location, `%LOCALAPPDATA%`, is non-roaming for exactly this reason.
- **Too many linked devices.** WhatsApp caps them (four at the time of writing)
  and drops the oldest. Remove unused devices on the phone.

---

## Reading messages

### The tool runs but returns no rows

In order of likelihood:

1. **"Only messages not returned by a previous run" is ticked and nothing is
   new.** That is the tool working correctly. Untick it to re-read history.
2. **Nothing has arrived since you linked.** A newly linked device gets messages
   from that moment on, never the account's past history. Send a test message.
3. **A chat filter matches nothing.** The run logs each unresolved entry. Clear
   **Only these chats** and check the **Chats** anchor for real ids.
4. **The date range excludes everything.** Timestamps are UTC.
5. **Both "Group chats" and "Direct chats" are unticked** — this is rejected
   with an explanation.

### Messages are missing

- **The sync stopped early.** If the log says it hit the limit, raise **Maximum
  time collecting**. Anything still queued arrives next run.
- **The quiet period is too short for a slow link.** Raise **Stop after quiet
  for**.
- **The device was unlinked for a while.** WhatsApp only queues for a limited
  period; messages older than that are gone from the stream. Nothing can recover
  them to a linked device.

### Attachments are not downloaded

- **"Download photos, videos and documents" is off.**
- **The file exceeded the size limit** — the run logs each skip with its size.
  Raise **Skip attachments larger than**.
- **The download failed** — logged per message. Usually a transient network
  problem; the message row is still archived and the file can be fetched by
  re-running with the watermark off.

### Blank rows, or a strange MessageType

Protocol noise is filtered, but a WhatsApp feature this version does not
recognise produces a row with the raw payload name in `MessageType` and an empty
`Body`. Tick **Include a RawJson column** to see what arrived, and filter those
rows out.

---

## Sending

### Every row fails with "not linked"

The Output tool's **Profile** or **data directory** does not match the one you
linked. They must both match the Input tool exactly.

### "does not match any known chat"

Chat names are resolved against the directory the **Input** tool builds. Run the
Input tool once, then use a name from its **Chats** anchor — or use the chat id,
which always works.

### "matches N different chats"

Two chats share that name. The tool will not guess; use the chat id. The error
lists the candidates.

### Numbers are rejected that look fine

- **Missing country code.** `5550100` is not routable. Either include it, or
  set **Default country code**.
- **A trunk zero with a country code already present** — `+1 0555 0100` is
  not a valid number.
- **The number genuinely has no WhatsApp account.** With **Check each number has
  WhatsApp** on, these are reported before sending rather than silently dropped.

### Sending is slow

By design. The default is 20 messages per minute, evenly spaced. Raise
**Messages per minute** if the recipients expect the traffic, and read
[Limits and legal](08-limits-and-legal.md) first.

### "WhatsApp is rate-limiting this account"

Lower the rate and retry later. Repeated rate limiting is a warning shot before
a suspension.

### "WhatsApp has temporarily banned this account"

Stop sending. The ban expires on its own; sending again immediately extends it.
Work out what looked like spam — usually volume, messaging people who never
messaged you, or identical text to many recipients — before resuming. See
[Limits and legal](08-limits-and-legal.md).

### Video or audio arrives as a document

Expected when FFmpeg is not installed. The complete file is delivered; the
recipient taps to download rather than playing it inline. Install FFmpeg and put
it on the `PATH` for inline playback. Text, images and documents are unaffected.

### Attachment "does not exist or is not a file"

- Check the path is complete and correct.
- Check the **Windows account running the workflow** can read it. On Alteryx
  Server that is a service account which usually cannot see mapped drive
  letters — use a UNC path such as `\\server\share\file.pdf`.

---

## Concurrency

### "Profile is already in use by process N"

A WhatsApp session can only be open in one process at a time. Either:

- put a **Block Until Done** between the Input and Output tools so they do not
  run together, or
- give one of them a **different profile** (each needs its own link), or
- stagger the schedules of two workflows.

If the named process is definitely gone, the lock is reclaimed automatically on
the next attempt. To force it, delete `profile.lock` from the profile folder —
but only once you are certain nothing is using it.

---

## Performance and disk

### The archive is growing

It keeps every message collected. To trim it:

```bash
"%APPDATA%\Alteryx\Tools\WhatsAppInput_1_0_0\whatsapp.cmd" prune --profile default --days 90
```

Downloaded media is left alone: a workflow may already have emitted
rows pointing at those files.

### The tools take about 240 MB each on disk

Expected. Each carries its own complete `site-packages` — pyarrow, pandas,
numpy, the SDK and the WhatsApp library — which is what makes the connector
work with nothing installed. Alteryx's own connectors are built the same way and
are a similar size.

---

## Reporting a bug

Include:

1. `doctor` output,
2. the exact message from the Results pane,
3. the connector version and Designer version,
4. what the tool was configured to do,
5. whether it ever worked, and what changed.

Never include `session.sqlite3` or its contents — it is a live credential for
the WhatsApp account.
