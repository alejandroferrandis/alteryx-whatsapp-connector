# WhatsApp Output

Sends one WhatsApp message for each incoming row.

**Anchors:** one input. One output — **Results** (R).

---

## How a run works

Rows are **buffered as they arrive and sent in `on_complete`**, never batch by
batch. That buys two things:

- the "does this number have WhatsApp?" check runs **once for the whole table**
  instead of once per row;
- the outbound rate limiter paces the **entire run** rather than restarting its
  clock on every batch.

The session is opened once, before the first send, so a problem with the profile
itself (not linked, unlinked, already in use) is reported once as a tool error —
not repeated as a thousand identical failed rows.

Every input row that is sent produces exactly one result row, in input order, whatever order
things failed in.

---

## Settings

### Connection

Identical to the Input tool — same fields, same meanings. Use the **same profile
name and data directory** in both tools to share one linked device.

You can link a device from this tool too: tick **Link this device**, enter the
number, run. Incoming data is ignored during a linking run.

### Send to

| Setting | What it does |
| --- | --- |
| **Take it from a column** | The destination comes from a column of your data. The usual choice. |
| **Use the same value for every row** | One fixed destination, typed once. Good for alerting a single group. |
| **Default country code for numbers without one** | Lets a column of local numbers work as-is. Digits only, no `+`. |

A destination can be any of:

| Form | Example | Notes |
| --- | --- | --- |
| **Phone number** | `+1 555 0100`, `0015550100`, `(555) 0100` | Spaces, dashes, dots and brackets are ignored. |
| **Chat id** | `120363000000000000@g.us`, `15550100@s.whatsapp.net` | Take it from the Input tool's **Chats** anchor, or its `ChatId` column. |
| **Chat name** | `Family`, `Ops team` | Resolved against the chat directory the Input tool builds. |

**The default country code only applies to numbers that clearly lack one** —
that is, no leading `+` and no `00`. A number that already carries a country
code is never prefixed again. A national trunk zero (`05550100`, `07700...`)
is stripped, because it is never part of the international form.

**Chat names must be unambiguous.** WhatsApp does not enforce unique names; if
two chats share one, the tool refuses that row and lists the candidate ids
rather than guessing. Silently picking one of two groups called "Team" is
exactly the bug that makes people stop trusting a tool.

Chat names only work after the **WhatsApp Input** tool has run at least once and
learned the directory.

### Message

Same two options: take the body from a column, or type one fixed message for
every row.

WhatsApp supports basic formatting in message text: `*bold*`, `_italic_`,
`~strikethrough~` and ` ```monospace``` `.

### Attachment (optional)

| Setting | What it does |
| --- | --- |
| **Column holding a file path** | Send a file with each row. Leave unset for text only. |
| **Column holding the attachment caption** | Caption for the file. Leave unset and the message text becomes the caption. |

How the file is sent depends on its extension:

| Extension | Sent as |
| --- | --- |
| `.jpg` `.jpeg` `.png` `.webp` | Image, previews inline |
| `.mp4` `.mov` `.mkv` `.3gp` `.avi` `.webm` | Video, plays inline — **needs FFmpeg**, see below |
| `.ogg` `.opus` `.mp3` `.m4a` `.wav` `.amr` `.aac` | Voice/audio message — **needs FFmpeg**, see below |
| anything else | Document, keeps its filename |

**Without FFmpeg installed**, video and audio files are sent as **documents**
instead. The recipient still receives the complete file; they tap to download it
rather than playing it inline. The run logs that this happened. Nothing else is
affected, and FFmpeg is never needed for text, images or documents. See
[README → What's bundled](../README.md#whats-bundled) for why it is not shipped.

**Paths must be readable by the account running the workflow.** On Alteryx
Server, mapped drive letters usually are not. Prefer a UNC path such as
`\\server\share\report.pdf`.

If you map a caption column *and* a message column with different text, the
file goes with the caption and the message follows as a second message.

### Advanced

| Setting | Default | What it does |
| --- | --- | --- |
| **Messages per minute** | 20 | Outbound pace. Sends are spread evenly; nothing is bursted. |
| **Send timeout (seconds)** | 60 | How long one send may take before it counts as failed. |
| **Connection timeout (seconds)** | 60 | How long to wait to reach WhatsApp. |
| **Check each number has WhatsApp before sending** | on | One batched lookup before sending. Numbers with no account are reported as failures instead of being messaged. |
| **Stop the workflow on the first failure** | off | Off means failures are reported per row and the run continues. |
| **Name shown on the phone under Linked devices** | `Alteryx` | |
| **Store sessions and archives in** | empty | Must match the Input tool. |
| **Proxy** | empty | `http://`, `https://` or `socks5://`. |

**About the pace.** The default of 20 per minute is gentle by design, and the
even spacing matters just as much: a token-bucket limiter would let the first
twenty messages go out back-to-back, which is exactly the burst pattern that
gets a number flagged. Raise it only for chats that expect the traffic. See
[Limits and legal](08-limits-and-legal.md).

The tool refuses to send more than **100,000 rows** in one run, and warns when
it truncates. At the default pace that would already take over three days.
Rows beyond the limit are neither sent nor reported, so on a truncated run the
Results anchor is shorter than the input - the warning says so explicitly.

---

## Results anchor

One row per input row that was sent, in input order. The only way to get fewer
rows than you put in is the 100,000-row cap above, which warns when it applies.

| Column | Type | Description |
| --- | --- | --- |
| `RowNumber` | int64 | 1-based position of the row in the input. |
| `SentTo` | text | The destination exactly as the row supplied it. |
| `ChatId` | text | What that destination resolved to. |
| `MessageId` | text | WhatsApp's id for the sent message. |
| `Success` | bool | True when WhatsApp accepted the message. |
| `Error` | text | Why it failed, and what to do about it. Empty on success. |
| `SentAt` | datetime | When the attempt finished, in UTC. |

`Success = True` means **WhatsApp accepted the message**. Whether it was
delivered or read is a separate question. Delivery receipts are a separate stream this version does not
expose.

Route failures somewhere useful:

```
WhatsApp Output ──► Filter (Success = False) ──► Email / log / retry table
                         │
                         └─ (True) ──► audit table
```

Every send attempt, successful or not, is also appended to an audit table inside
the profile's `archive.sqlite3` (`outbox`), with a preview of the body. Useful
the first time someone asks "did we really send that?".

---

## Recipes

**Alert a group when a check fails**

Filter your data down to failures, then Output with **Send to** = *fixed* set to
the group's chat id, and **Message** built by a Formula tool.

**Reply to whatever came in**

Feed the Input tool's Messages anchor through a Filter, then set **Send to** to
the `ChatId` column. Replies land in the right chat, group or direct, with no
extra logic.

**Send a personalised report to each customer**

A table of `Phone`, `Message` and `ReportPath`. Map all three. Set **Default
country code** if your numbers are stored nationally. Leave the rate at 20/min
and let it run.

**Dry run before sending for real**

Set **Messages per minute** to 1 and filter the input to a single test row.
Check the Results anchor before removing the filter.

---

## Errors you might see

| Message | What to do |
| --- | --- |
| `'X' is not a phone number and does not match any known chat` | Use a full number with country code, or a chat id. Run the Input tool once so chat names are known. |
| `'X' matches N different chats` | Two chats share that name. Use the chat id instead; the message lists the candidates. |
| `+N does not have a WhatsApp account.` | The number is not on WhatsApp. Check the country code and the digits. |
| `The attachment '...' does not exist or is not a file.` | Check the path, and that the workflow's Windows account can read it. Use UNC paths on a server. |
| `WhatsApp is rate-limiting this account` | Lower **Messages per minute** and retry later. |
| `WhatsApp has temporarily banned this account` | Stop. Read [Limits and legal](08-limits-and-legal.md) before sending again. |
| `This row has neither a message nor an attachment` | Both the body and the attachment resolved to empty for that row. |
| `The incoming data has no column called 'X'` | A selected column is not in the data. The warning lists the columns that are. |

Fuller list: [Troubleshooting](07-troubleshooting.md).
