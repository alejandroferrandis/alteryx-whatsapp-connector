# Linking a device

How the connector authenticates, what a profile is, and how to handle the things
that go wrong.

---

## What "linking" actually means

The connector uses WhatsApp's **multi-device** protocol — the same mechanism
behind WhatsApp Web and the desktop app. Your phone remains the owner of the
account. This machine becomes an additional **linked device**, with its own
encryption keys, able to send and to receive new messages independently of
whether the phone is online.

Consequences worth understanding up front:

- **You are not giving the connector your password.** There isn't one. Linking
  exchanges keys with the phone, and the phone can revoke the device at any time
  from **Settings → Linked devices**.
- **The link is a credential.** `session.sqlite3` in the profile folder *is* the
  device. Anyone who copies that file can read and send as the account. Treat it
  like a private key; see [Protecting the session](#protecting-the-session).
- **WhatsApp allows a limited number of linked devices** per account (four at
  the time of writing). Each profile on each machine uses one.
- **History is not transferred.** A newly linked device receives messages that
  arrive *after* it was linked; the account's past conversations stay behind. The
  connector builds its own history from that point forward.

---

## Profiles

A **profile** is one linked device, identified by a name you choose. It owns a
folder:

```
%LOCALAPPDATA%\Alteryx\WhatsAppConnector\profiles\<profile>\
    session.sqlite3     the link itself
    archive.sqlite3     messages collected so far, and the chat directory
    media\              attachments downloaded from incoming messages
    profile.json        which account is linked, and when
    profile.lock        present only while a tool is using the profile
```

Most installations need exactly one, called `default`. You need more than one
when:

- you read from **two different WhatsApp accounts** (support line and sales
  line, say) — one profile each;
- you want a **test account** whose messages never mix with production;
- two workflows must run **at the same time** — see
  [Concurrency](#concurrency-one-workflow-at-a-time-per-profile).

To use a second profile, type a new name into **Profile** in either tool and
link it. The folder is created automatically.

> Input and Output share a linked device only if they have **the same profile
> name and the same data directory**. Mismatching them is the most common setup
> mistake: the Output tool reports "not linked" while the Input tool works
> perfectly.

---

## Linking with a phone-number code (recommended)

This is the default because it needs nothing but the Designer window.

1. Tick **Link this device** in the Connection section of either tool.
2. Enter the account's phone number, with country code. Spaces, dashes, brackets
   and a leading `+` or `00` are all fine — `+1 555 0100`,
   `001-555-0100` and `+1(555)0100` are the same number.
3. Run the workflow.
4. Read the 8-character code from the Results pane.
5. On the phone: **WhatsApp → Settings → Linked devices → Link a device → Link
   with phone number instead**, then type the code.
6. **Untick "Link this device"** and run again to do real work.

The code is valid for roughly a minute. If it expires, run the workflow again.

---

## Linking with a QR code

Leave **Phone number to link** empty and the tool falls back to a QR code. It is
printed as text art in the Results pane and also saved as
`link-qr.png` in the profile folder, because a QR code rendered in a log is
rarely scannable.

Scan it from the phone under **Settings → Linked devices → Link a device**.

Use this only if the phone's WhatsApp build has no "Link with phone number"
option. The code flow is easier and far less likely to generate a support
question.

---

## Re-linking after WhatsApp unlinks the device

WhatsApp revokes linked devices for several ordinary reasons: someone removed it
on the phone, the account was logged out everywhere, the device was idle for a
long time, or WhatsApp's own security heuristics fired.

When that happens the tools report:

> WhatsApp has unlinked profile "default" (the device was removed from the
> phone, or the account was logged out). Fix: re-link the device.

The stored session is dead; no amount of retrying revives it. To recover:

1. Tick **Link this device**.
2. Tick **Replace the existing link**. (Without this, the tool sees a session
   file, assumes you are already linked, and does nothing.)
3. Enter the phone number and run.
4. Untick both and carry on.

**Your archive survives.** Re-linking replaces the credential only; every
message previously collected stays in `archive.sqlite3`, and the chat directory
with it.

---

## Concurrency: one workflow at a time, per profile

WhatsApp's device state is a single-writer store. Two processes using one linked
device at the same time corrupt it, and WhatsApp responds by unlinking the
device.

The connector prevents this with a lock file. A second tool trying to use a busy
profile fails immediately and clearly:

> Profile "default" is already in use by process 12345 on WORKSTATION.

If the owning process has died, the lock is reclaimed automatically — a crashed
workflow never wedges a profile permanently.

What this means in practice:

- A WhatsApp Input and a WhatsApp Output tool in the **same workflow** will
  conflict if Designer runs them simultaneously. Put a **Block Until Done**
  between them, or give them separate profiles.
- **Two scheduled workflows** on the same profile must not overlap. Stagger
  them, or use one workflow that reads and sends in sequence.

---

## Protecting the session

`session.sqlite3` grants full send-and-receive access to the WhatsApp account,
with no further authentication. Handle it accordingly:

- It lives under `%LOCALAPPDATA%`, **not** `%APPDATA%`, specifically so it does
  not follow a roaming Windows profile onto another machine. Two live copies of
  one device get both of them unlinked.
- Do not put the profile folder on a network share, in OneDrive, Dropbox, or any
  backup that other people can read.
- On a shared server, run the workflow under a dedicated service account so the
  profile folder is not readable by other users.
- To revoke access, remove the device from the phone
  (**Settings → Linked devices**). Deleting the local files stops this machine
  using it but does not revoke it on WhatsApp's side.

---

## Unlinking

**From the phone** (this is the real revocation): **WhatsApp → Settings →
Linked devices**, select the device, **Log out**.

**Locally**, to remove the session from this machine while keeping the archive:

```bash
"%APPDATA%\Alteryx\Tools\WhatsAppInput_1_0_0\whatsapp.cmd" unlink --profile default
```

Or simply delete `session.sqlite3` from the profile folder.

---

## Checking state without running a workflow

```bash
"%APPDATA%\Alteryx\Tools\WhatsAppInput_1_0_0\whatsapp.cmd" doctor
"%APPDATA%\Alteryx\Tools\WhatsAppInput_1_0_0\whatsapp.cmd" status --profile default
```

`doctor` verifies the installation — that the native library loads, whether
FFmpeg is present, and which profiles exist. `status` prints one profile's
linked account, message counts and folder. Both are the fastest way to answer
"is it linked?" and the first thing to attach to a bug report.

---

## Device name

**Advanced → Name shown on the phone under Linked devices** controls the label
the account holder sees in WhatsApp's device list. It defaults to `Alteryx`.
Set it to something recognisable when several machines link the same account —
`Alteryx PROD` and `Alteryx TEST` are much easier to tell apart than two
identical entries.
