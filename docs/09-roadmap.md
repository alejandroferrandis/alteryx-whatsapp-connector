# Roadmap

Planned work beyond 1.0, with enough design detail that each item can be picked
up without re-deriving it. Where something is unproven it says so; nothing here
is promised until its unknown is closed.

Ordered by value for effort.

---

## ~~1.1 — Ingestion time windows~~ — shipped in 1.0

**Status:** done. Kept here because the reasoning still explains the settings.

### The problem

A linked device that has not connected for a month receives **everything
WhatsApp queued for it** the moment it reconnects. Nothing in 1.0 stops that
landing in the archive and on the output anchor. A workflow that was paused over
a holiday comes back and floods.

Separately, on a fresh link there is no way to say "I only care about what
happens from now on" — 1.0 takes whatever WhatsApp offers.

### The design

Both are the same mechanism: **a floor on what gets ingested**, applied in the
sync handler before a message reaches the archive.

Two settings, in the Input tool's *What to read* section:

| Setting | Default | Behaviour |
| --- | --- | --- |
| **Ignore messages older than (days)** | 7 | Any message whose timestamp is older than the cutoff is counted and discarded rather than archived. 0 disables the limit. |
| **Only read messages sent after this device was linked** | on | Ignore anything older than the moment the device was linked. Makes "don't scrape history" the default. |

As built, the mark is stored as `first_sync_utc` and the two limits compose:
whichever is *later* governs.

The mark is seeded from the profile's `linked_at`, **not** from the clock at
first sync. The difference is not cosmetic: seeding it during the first sync
puts it after any message sent in the gap between linking and running the
workflow, which is precisely the test message every new user sends themselves.
A profile written before `linked_at` existed falls back to the first-sync clock. Unticking the setting stops the
mark being applied without erasing it, so the backlog can be taken later without
resetting the profile.

Implementation notes:

- Apply the floor in `runner._sync`'s `handle()`, alongside the existing
  `is_ignorable` check, so discarded messages never reach `store.add_messages`.
- Seed the mark from `linked_at` when the profile has one, so the window opens
  at link time rather than at first run.
- Store `first_sync_utc` in the archive's `meta` table, alongside the data it
  filters —
  it belongs with the data it filters, and survives a re-link.
- **Count and report what was dropped.** `SyncStats` gains a `too_old` field and
  the summary line says so. Silently discarding messages is exactly the kind of
  behaviour that destroys trust in a connector; the user must see
  `ignored 412 message(s) older than 7 days` in the Results pane.
- The floor filters *ingestion* only. Messages already archived stay
  archived — lowering the setting must never delete history.

### Effort

About an hour including tests and the two UI fields.

---

## 2.0 — Dynamic chat picker in the configuration panel

**Status:** design sketched, **one unknown to spike before committing**.

### The problem

Finding a chat id means running the Input tool, browsing the Chats anchor, and
copying a string like `120363000000000000@g.us` into a text box. It works — it
is how 1.0 solves the problem — but it is clerical, and on a real account the
directory is over a thousand rows.

What we want: open the panel, see a list of your actual chats, tick the ones you
care about.

### The constraint

The configuration panel is **sandboxed JavaScript**. It cannot run Python, query
the archive, or read the filesystem. Any data it shows has to arrive through the
tool's own configuration XML.

### The design

The engine writes the chat directory back into the tool's own configuration
after a sync, and the panel reads it on open:

1. After a successful sync, the Input plugin calls
   `provider.save_tool_config({... "ChatCache": [...]})` with a compact list of
   `{id, name, isGroup}`.
2. Designer persists that into the node's `<Configuration>`.
3. Next time the panel opens, `Alteryx.Gui.BeforeLoad` receives it in
   `json.Configuration.ChatCache` and populates a `ListBox` bound to a
   `StringSelectorMulti` data item, instead of the current free-text box.
4. The free-text box stays as a fallback, for ids not in the cache and for
   workflows edited without ever running.

### The unknown — spike this first

**Does `save_tool_config()` persist to the saved workflow when called during a
normal run?** The SDK method exists and pushes a `save_config` control message,
but it is not established that Designer writes it into the node rather than
discarding it after the run, nor whether it marks the workflow dirty so the user
is prompted to save.

Spike: have the Input tool write a counter into its config on every run, run it
three times, save and reopen the workflow, and see what survived. An afternoon.

**If it does not persist,** the fallbacks in order of preference are:

- have the panel read a JSON file the engine writes, if CEF permits
  `file://` XHR from the panel's origin — needs its own test;
- keep the free-text box and add a **Chats** helper output that is easier to
  copy from, which is a smaller win but certain to work.

### Also worth knowing

The picker cannot populate before the **first** sync — there is nothing to show
until the connector has seen the account. So the flow is "link → run once →
reopen the panel → pick chats". Better than copying ids, but not magic, and the
panel should say so rather than looking broken on a fresh tool.

---

## 2.0 — A proper linking window with a live QR code

**Status:** design settled, **feasibility confirmed**.

### The problem

1.0 links a device by printing an 8-character code into the Results pane, which
works and needs no terminal. The QR alternative is weaker: it renders as text
art in the log and as a PNG written to the profile folder, neither of which is
pleasant to scan.

A first-run window showing a real, live QR code is the experience people expect,
because it is what WhatsApp Web does.

### The constraint, and why the obvious approaches fail

- **A native window is out.** Designer's embedded Python has **no `tkinter`**
  (verified — embedded distributions strip it). A GUI toolkit would have to be
  bundled, adding tens of megabytes for one dialog.
- **Rendering the QR in the configuration panel does not work either.** A
  WhatsApp QR **rotates roughly every 20 seconds** and expires in about a
  minute. Getting it into the panel via `save_tool_config` means a round trip
  through a workflow run per refresh — orders of magnitude too slow. The panel
  also has no way to start a pairing session or know one is in progress.

### The design that does work

`webbrowser` and `http.server` **are** both available in the embedded
interpreter (verified). So:

1. The tool, or a Start-menu shortcut, starts a **loopback HTTP server on a
   random port** and opens the default browser at it.
2. The page shows the live QR as an inline `data:` URI, plus the phone-number
   code option alongside it, plus a plain-English explanation of where to look
   on the phone.
3. The page polls a small JSON endpoint; when WhatsApp rotates the QR the image
   updates in place, and when pairing completes the page says so and the server
   shuts down.
4. `segno` is already bundled for the existing QR support, so rendering costs
   nothing extra.

This gives a real window, a live QR, no bundled GUI toolkit and no new
dependency.

Security notes, to be honoured in the implementation:

- Bind **127.0.0.1 only**, never `0.0.0.0`.
- Random port, and a random token in the URL, so nothing else on the machine can
  read the QR — a QR code is a **credential**: anyone who scans it links
  *their* device to the account.
- Shut the server down on pairing, on timeout, and on tool exit.
- Never serve anything but the pairing page.

### Effort

A day, most of it in the page and in shutdown handling.

---

## Smaller items

| Item | Notes |
| --- | --- |
| **Delivery and read receipts** | whatsmeow emits `Receipt` events. Would need a second archive table and probably a third output anchor. Frequently the first thing anyone asks for after sending works. |
| **Reply to a specific message** | The library supports quoting; needs a `QuotedMessageId` input column on the Output tool. Small, and makes threaded bots much better. |
| **Reactions, edits, deletions** | All supported by the library, all need new Output tool modes. |
| **Group administration** | Create groups, add and remove members, change subjects. A separate tool rather than settings on the existing two. |
| **Archive retention setting** | `prune` exists in the CLI; expose "keep messages for N days" in the Input tool so it happens automatically. |
| **DCM integration** | Store the profile reference in Alteryx's Data Connection Manager rather than a plain profile name, for shared and server deployments. |
| **Alteryx Server support** | Needs thinking about where profiles live for a service account, and about the single-writer lock when several workers share a machine. Not just a packaging change. |
| **Bundled FFmpeg** | Would remove the video/audio-as-document fallback. Held back on licence grounds rather than technical ones — see [Limits and legal](08-limits-and-legal.md). Bundling an LGPL or GPL build would impose obligations on everyone who redistributes the connector. |

---

## Explicitly not planned

- **Anything that helps send unsolicited messages in bulk.** No list import
  wizard, no blast mode, no send-to-every-contact. The rate limiter and the
  recipient check are there for a reason, and features that undermine them get
  accounts banned and do real harm to the people receiving them.
- **Reading other people's linked devices,** or anything that touches an account
  the user has not linked themselves.
