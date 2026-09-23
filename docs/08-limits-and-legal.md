# Limits and legal

Read this before deploying the connector. None of it is a reason not to use the
tool; all of it is something you should decide knowingly rather than discover.

---

## What protocol this uses

The connector speaks WhatsApp's **multi-device Web protocol** — the same one
behind WhatsApp Web and the desktop app — by linking a device to an ordinary
WhatsApp account.

It is **not** the WhatsApp Business Platform (Cloud API), Meta's official,
paid, per-conversation product. The practical differences:

| | This connector | WhatsApp Business Platform |
| --- | --- | --- |
| Account | Any ordinary WhatsApp account | A registered business account |
| Cost | None beyond the tool | Per conversation, billed by Meta |
| Approval | None | Business verification; templates pre-approved |
| Sanctioned by Meta | **No** | Yes |
| Message templates | Not required | Required to start a conversation |
| Ban risk | Real — see below | Low, if you follow the rules |
| Setup | Link a device, minutes | Days to weeks |

If you are sending marketing messages at scale, or to people who did not message
you first, the official platform is the correct tool and this one is not.

For operational use — an internal alerting channel, a support inbox, a team
group, replying to people who wrote to you — the multi-device approach is
proportionate and is what makes this a five-minute setup instead of a project.

---

## Terms of service

Automating an ordinary WhatsApp account through an unofficial client is
**contrary to WhatsApp's Terms of Service**, which reserve access to WhatsApp's
own clients and prohibit automated or bulk messaging.

Meta enforces this, and it does so at the account level: a number can be
temporarily restricted or permanently banned. There is no notice period and
appeals rarely succeed.

**This is your decision to make, and it is worth making it knowingly:**

- Do not use it for unsolicited messaging, marketing to purchased lists, or
  anything a recipient would call spam. This is both the fastest way to get
  banned and the actual harm the rules exist to prevent.
- Prefer a **dedicated number** whose loss would be an inconvenience rather than
  a business interruption.
- Do not link a number your business depends on for customer contact.
- Tell the people in any automated chat that they are talking to an automated
  system.
- Check your obligations under GDPR or local privacy law before archiving other
  people's messages — the connector stores message content and phone numbers on
  disk, which makes you a data controller.

---

## Staying within reasonable limits

WhatsApp publishes no numbers. What follows is conservative practice; no
allowance is documented anywhere.

**Rate.** The Output tool defaults to **20 messages per minute**, evenly spaced.
Even spacing matters as much as the rate: a burst of twenty followed by silence
looks like a broadcaster, while a steady trickle looks like a person. Raise it
only for chats that expect the traffic.

**Who you message.** The strongest signal against you is messaging people who
have never messaged you. Recipients blocking or reporting the number is what
turns a rate limit into a ban. Replying to inbound conversations is far safer
than initiating.

**Variety.** Identical text to many recipients is a spam signature. Personalise
where you can — you have Alteryx, so this is easy.

**Volume growth.** A brand-new number that immediately sends hundreds of
messages is the classic spam pattern. Start small and grow over days.

**Group messages** are generally safer than direct messages to strangers, since
membership implies consent.

### Warning signs

| Signal | What it means |
| --- | --- |
| `WhatsApp is rate-limiting this account` | Slow down now. This is the warning shot. |
| `WhatsApp has temporarily banned this account` | Stop. Sending again immediately extends it. Work out what looked like spam first. |
| Device unlinked repeatedly | May be WhatsApp's security heuristics, or two processes sharing a session — see [Troubleshooting](07-troubleshooting.md). |

---

## What this version does not do

Scope, so expectations are accurate:

- **No delivery or read receipts.** `Success = True` means WhatsApp accepted the
  message. Arrival and reading are not reported.
- **No message history before linking.** A linked device receives what arrives
  after it is linked. The connector's history starts then.
- **No editing, deleting or reacting** to messages from a workflow.
- **No group administration** — creating groups, adding members, changing
  subjects.
- **No calls, status updates or channels.**
- **No inline playback for video or audio sent without FFmpeg** — such files are
  delivered as documents.
- **Windows only.** The engine is portable, but the package and the tested build
  target Designer on Windows x64.

Several of these are supported by the underlying library and could be added.

---

## Data and privacy

What is stored on disk, per profile, under
`%LOCALAPPDATA%\Alteryx\WhatsAppConnector\profiles\<profile>\`:

| File | Contents | Sensitivity |
| --- | --- | --- |
| `session.sqlite3` | The linked device and its encryption keys | **Credential.** Full send/receive access to the account. |
| `archive.sqlite3` | Message text, sender numbers and names, timestamps; an audit log of everything sent | Personal data. |
| `media/` | Files received in messages | Personal data. |
| `profile.json` | Which account is linked | Low. |

Consequences:

- **The archive is personal data** about people who did not choose to be in your
  data warehouse. Set a retention period and enforce it:
  `whatsapp.cmd prune --days N`.
- **`session.sqlite3` is a credential.** Never put it on a share, in a synced
  folder, or in a backup others can read. To revoke it, remove the device from
  the phone — deleting the file only stops *this machine* using it.
- **Messages are end-to-end encrypted in transit.** The connector is a
  legitimate endpoint holding its own keys, so it decrypts them; nothing passes
  through any third-party server. The plaintext then sits in your archive, which
  is not encrypted at rest — use BitLocker or an encrypted volume if that
  matters.
- On a shared server, run the workflow under a dedicated account so the profile
  folder is not readable by other users.

---

## Licences

**The connector itself is MIT** — see `LICENSE`. The components below are
bundled inside the `.yxi` under their own licences; the full list, and the
MPL-2.0 notice that whatsmeow requires, are in `THIRD-PARTY-NOTICES.md`, which
ships inside the package.

| Component | Purpose | Licence |
| --- | --- | --- |
| [neonize](https://github.com/krypton-byte/neonize) | WhatsApp multi-device protocol (Python bindings) | Apache-2.0 |
| [whatsmeow](https://github.com/tulir/whatsmeow) | The Go core inside neonize | MPL-2.0 |
| [ayx-python-sdk](https://pypi.org/project/ayx-python-sdk/) | Alteryx plugin runtime | Alteryx SDK and API License Agreement |
| pyarrow, pandas, numpy | Record transport | Apache-2.0 / BSD-3-Clause |
| grpcio, protobuf | SDK transport | Apache-2.0 / BSD-3-Clause |
| phonenumbers | Number parsing | Apache-2.0 |
| Pillow, segno, httpx, requests and others | Supporting libraries | MIT / Apache-2.0 / BSD / HPND |

**FFmpeg is not bundled** and is not required. It is optional, and only for
sending video or audio as playable messages rather than documents. It is omitted
on purpose: its redistributable builds carry LGPL or GPL obligations that do
not belong inside a packaged Alteryx tool, and shipping the wrong build would
place obligations on anyone who redistributes this connector.

If you redistribute the `.yxi`, review those licences — in particular the
**Alteryx SDK and API License Agreement**, which governs bundling their SDK,
and **MPL-2.0** for whatsmeow, which requires that its source remain available
and that modifications to its files be published. Redistributing the *source*
alone carries none of this, since the dependencies are not in the repository.

---

## Trademarks

**WhatsApp** is a trademark of WhatsApp LLC / Meta Platforms, Inc. This product
is not affiliated with, endorsed by, sponsored by or connected to them in any
way. The name is used only to describe what the connector interoperates with,
which is nominative fair use.

The tool icons are generic speech bubbles and do not imitate the
WhatsApp mark.

**Alteryx** and **Alteryx Designer** are trademarks of Alteryx, Inc. This
connector is a third-party product built on their published SDK.

---

## No warranty

This connector is provided as-is. It depends on an undocumented protocol that
Meta can change without notice, and on account access Meta can withdraw without
notice. Neither the availability of the service nor the continued operation of
any linked account is guaranteed. Do not build a process on it that cannot
tolerate an outage.
