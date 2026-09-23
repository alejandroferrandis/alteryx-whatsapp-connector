# WhatsApp Input

Reads messages from a linked WhatsApp account into a workflow.

**Anchors:** no input. Two outputs — **Messages** (M) and **Chats** (C).

---

## How a run works

Each run does two separable things:

1. **Sync.** Connect to WhatsApp, receive everything it has been holding for
   this device since the last connection, and write it to a local SQLite
   archive. WhatsApp delivers the backlog in a burst and then goes quiet, which
   is how the tool knows it has everything.
2. **Emit.** Query that archive — by chat, by date, by "not seen before" — and
   put the result on the output anchor.

Splitting them is what makes the tool behave sensibly in a workflow:

- **Re-running is safe.** The archive is the source, so a second run returns the
  same rows rather than nothing.
- **A failure costs nothing.** Messages are durable before they reach the
  anchor; a workflow that dies downstream re-reads them next time.
- **You can ask for history.** "Everything from the Ops group last Tuesday" is a
  query - something WhatsApp itself could never answer.
- **Nothing is missed between runs.** WhatsApp queues messages for an offline
  linked device and delivers them on reconnect, so an hourly schedule loses
  nothing.

The archive is the tool's memory; if you delete it, you lose collected history
but not the link.

---

## Settings

### Connection

| Setting | Default | What it does |
| --- | --- | --- |
| **Profile** | `default` | Which linked device to use. Must match the Output tool to share a link. See [Linking a device](02-linking-a-device.md). |
| **Link this device** | off | Turns this run into a pairing run: no messages are read. Untick it afterwards. |
| **Phone number to link** | empty | The account to link, with country code. Empty falls back to a QR code. |
| **Replace the existing link** | off | Discards the stored session first. Needed after WhatsApp unlinks the device. |

### What to read

| Setting | Default | What it does |
| --- | --- | --- |
| **Source** | Sync | *Sync* connects and collects, then returns. *Archive only* re-reads what earlier runs collected without connecting — useful offline, for reprocessing, or to avoid a second connection in a workflow that already has one. |
| **Only these chats** | empty | Restrict to specific chats. One per line or comma-separated. Accepts chat names, phone numbers and chat ids, mixed. Empty means every chat. |
| **Group chats** / **Direct chats** | both on | Which kinds of conversation to return. Turning both off is rejected — it could never return a row. |
| **Messages I sent** | off | Include messages sent *from* this account, including from the phone. Useful for a full transcript; noisy for a bot. |
| **Only messages not returned by a previous run** | on | The watermark. Each run returns only what is new. This is what makes a scheduled workflow work. |
| **Only read messages sent after this device was linked** | on | The moment you linked marks the start of time; anything older is never archived. Stops a fresh link importing whatever backlog WhatsApp had queued. Messages sent after linking still arrive, even if the workflow had not run yet. |
| **Ignore messages older than (days)** | 7 | A rolling limit on how far back a sync may reach. 0 lifts it. |
| **Limit to a date range** | off | Turns the From/To bounds below on. They are ignored while this is off, because Alteryx's date widget pre-fills itself with today. |
| **From** / **To** | empty | Restrict by message timestamp. Inclusive whole days in local time; the run log prints the UTC window actually used. |
| **Maximum rows** | 0 | Cap the output. 0 means no limit. Applied after ordering, so you get the *oldest* N. |

The watermark moves only after the rows have actually been written to the
anchor, so an interrupted run re-reads rather than skips.

**The two age limits are about ingestion, the date range is about output.** A
message older than the floor is never written to the archive at all; the date
range filters what an already-populated archive returns. So tightening an age
limit cannot lose history you have already collected, and loosening one cannot
recover messages WhatsApp has since stopped queueing.

Anything dropped for age is counted and reported:

```
Sync finished in 6.1s: received 412, new 18, too old 394.
394 message(s) were older than the limit and were not archived.
```

Messages are never discarded silently - a connector that quietly drops data is
one nobody can trust.

To reprocess history, untick **Only messages not returned by a previous run**
and use the date range.

### Attachments

| Setting | Default | What it does |
| --- | --- | --- |
| **Download photos, videos and documents** | on | Save incoming files to the profile's `media` folder and put the path in `MediaPath`. |
| **Skip attachments larger than (MB)** | 25 | Files above this are archived as messages with `HasMedia = True` but no `MediaPath`, and the run logs why. 0 disables the limit. |

Files are named after the chat *and* the message id - WhatsApp only guarantees a
message id is unique within its own chat, so the chat has to be part of the name
for two conversations not to collide on one file. Re-running a sync reuses the
file already on disk rather than downloading it again.

### Advanced

| Setting | Default | What it does |
| --- | --- | --- |
| **Stop after quiet for (seconds)** | 8 | End collection after this long with nothing arriving. The backlog arrives in a burst, so a short quiet period reliably means "that was all". Raise it on a slow link. |
| **Maximum time collecting (seconds)** | 120 | Hard ceiling on one sync, so a very busy account cannot stall a workflow. Anything still queued arrives on the next run. |
| **Connection timeout (seconds)** | 60 | How long to wait to reach WhatsApp before giving up. |
| **Publish the chat directory on the Chats anchor** | on | Turn off if you do not use it. |
| **Include a RawJson column** | off | Adds routing metadata as JSON for troubleshooting. Contains no message content beyond what other columns already carry. |
| **Name shown on the phone under Linked devices** | `Alteryx` | The label in WhatsApp's device list. |
| **Store sessions and archives in** | empty | Override the default location. Must match the Output tool. |
| **Proxy** | empty | `http://host:3128`, `https://...` or `socks5://host:1080`. |

The tool stops collecting at whichever comes first: WhatsApp's own
"you are up to date" signal, the quiet period, or the maximum time.

---

## Messages anchor

One row per message, **oldest first** — so a workflow that replies processes a
conversation in the order it happened.

| Column | Type | Description |
| --- | --- | --- |
| `MessageId` | text | WhatsApp's id for this message. Unique within a chat; use it with ChatId as a compound key. |
| `ChatId` | text | The conversation this belongs to. Ends in @g.us for a group, @s.whatsapp.net for a direct chat. Paste this into the Output tool to reply. |
| `ChatName` | text | Friendly name of the chat, resolved at read time so renames apply to history too. |
| `IsGroup` | bool | True when the message came from a group. |
| `SenderId` | text | Who sent it. In a group this differs from ChatId. |
| `SenderName` | text | The sender's WhatsApp display name at the time they sent it. |
| `FromMe` | bool | True when this linked account sent the message rather than received it. |
| `Timestamp` | datetime | When WhatsApp recorded the message, in UTC. |
| `Body` | text | The text, or the caption of a photo, video or document. |
| `MessageType` | text | text, image, video, audio, document, sticker, location, contact, poll, reaction, or the raw payload name for anything unrecognised. |
| `HasMedia` | bool | True when the message carried a file. |
| `MediaPath` | text | Full path to the downloaded file, or empty if downloads are off or the file exceeded the size limit. |
| `MediaMime` | text | MIME type WhatsApp reported for the file. |
| `MediaSize` | int64 | File size in bytes, as reported by WhatsApp. |
| `QuotedMessageId` | text | The MessageId this one replies to, if it is a reply. |
| `IsForwarded` | bool | True when the message was forwarded. |
| `RawJson` | text | Routing metadata as JSON, for debugging. Empty unless 'Include raw JSON' is ticked. |

**Timestamps are UTC.** Convert with a DateTime tool if your workflow reports in
local time.

**Protocol noise is filtered out.** Delivery receipts, key rotations, deletions
and encrypted poll votes never reach the anchor; they would otherwise appear as
a stream of blank rows.

**An unknown message type still produces a row.** `MessageType` carries the raw
payload name and `Body` is empty, so a new WhatsApp feature shows up as
something you can see and filter rather than silently vanishing.

---

## Chats anchor

The directory of every chat the account can address. **This is where chat ids
come from** — nobody can guess that their family group is
`120363000000000000@g.us`.

| Column | Type | Description |
| --- | --- | --- |
| `ChatId` | text | The id to use when sending to this chat. |
| `ChatName` | text | Group subject, or the contact's name. |
| `IsGroup` | bool | True for a group, false for a direct chat. |
| `ParticipantCount` | int64 | Members in the group. 0 for direct chats. |
| `LastMessage` | datetime | When this chat last produced a message in the archive, in UTC. |

Most recently active first.

The directory is refreshed on every sync and is also what lets the Output tool
accept a **chat name** as a destination. A name it has never seen cannot be
resolved — run the Input tool once first.

---

## Recipes

**Only new messages from one group**

Put the group's `ChatId` (from the Chats anchor) in **Only these chats**, leave
**Only messages not returned by a previous run** ticked, and schedule it.

**Everything from last week, again**

Untick **Only messages not returned by a previous run**, set **From** and
**To**, and set **Source** to *Archive only* so it does not connect at all.

**Just the attachments**

Filter on `HasMedia = True` and use `MediaPath`. A Directory or Blob Input tool
downstream can pick the files up from there.

**Replies only**

Filter on `QuotedMessageId != ""`, then join back to the Messages anchor on
`MessageId` to pull in what was replied to.

**Find a chat id**

Run with **Only these chats** empty, browse the **Chats** anchor, copy the id.

---

## Errors you might see

| Message | What to do |
| --- | --- |
| `This tool is not connected to WhatsApp yet...` | Follow the numbered steps it prints. See [Linking a device](02-linking-a-device.md). |
| `WhatsApp has unlinked profile "x"` | Re-link with **Replace the existing link** ticked. |
| `Profile "x" is already in use by process N` | Another workflow holds it. Serialise them, or use a second profile. |
| `Could not reach WhatsApp within N seconds` | Check internet access and any proxy or firewall. WhatsApp Web needs a WebSocket to `*.whatsapp.net` on 443. |
| `'X' does not match any known chat yet` | The chat name is unknown. Run once with **Only these chats** empty and take the id from the Chats anchor. |

Fuller list: [Troubleshooting](07-troubleshooting.md).
