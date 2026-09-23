# Quick start

Fifteen minutes from a downloaded `.yxi` to a workflow that reads and replies to
WhatsApp messages.

---

## 1. Install

1. **Close Alteryx Designer.** The installer cannot replace files that Designer
   has open, and a half-installed tool is confusing to diagnose.
2. Double-click `WhatsApp_1_0_0.yxi`.
3. Accept the prompt. Installing for **the current user** is the normal choice;
   install for all users only if several Windows accounts on this machine need
   the tools.
4. Reopen Designer. Under the **Connectors** category you now have **WhatsApp
   Input** and **WhatsApp Output**.

Nothing else is installed. No services, no PATH changes, no Python environment.

> If the tools do not appear, see
> [Troubleshooting → The tools do not appear](07-troubleshooting.md#the-tools-do-not-appear-after-installing).

---

## 2. Choose an account

The connector links to WhatsApp the same way WhatsApp Web does: your phone stays
the owner of the account, and this machine becomes one of its **linked devices**.

Two decisions worth making now rather than later:

- **Which number.** A dedicated number kept for automation is the safer choice.
  Everything this connector does is subject to WhatsApp's anti-spam enforcement,
  and a suspension on a shared business number is painful. See
  [Limits and legal](08-limits-and-legal.md).
- **Whose phone.** Linking needs one-time physical access to the phone that owns
  the account. After that the phone can be offline; the link survives on its
  own.

The account keeps working normally on the phone throughout. A linked device sees
new messages and can send, exactly like WhatsApp Web.

---

## 3. Link the device

1. Drag **WhatsApp Input** onto an empty canvas.
2. In the **Connection** section:
   - leave **Profile** as `default`,
   - tick **Link this device**,
   - type the account's phone number with its country code, e.g.
     `+1 555 0100`.
3. Run the workflow (**Ctrl+R**).
4. Watch the **Results** pane. Within a few seconds it prints:

   ```
   ==========================================================
     LINK CODE:   A1B2C3D4
   ==========================================================
     On the phone that owns this WhatsApp account, open:
       WhatsApp > Settings > Linked devices > Link a device
       > Link with phone number instead
     then type the code above. Waiting up to 180 seconds...
   ```

5. On the phone, follow exactly that path and type the code.
6. The Results pane confirms `Linked successfully`.

7. **Untick "Link this device".** This is the step people forget: while it is
   ticked, every run tries to link instead of reading messages.

Codes expire after about a minute. If yours does, just run the workflow again
for a fresh one.

More on profiles, re-linking and multiple accounts:
[Linking a device](02-linking-a-device.md).

---

## 4. Read your first messages

With **Link this device** unticked, run the workflow again.

The tool connects, collects everything WhatsApp has been holding for this device
since it was linked, stores it locally, and returns it.

- The **Messages** anchor (M) has one row per message — who, when, what, and the
  path to any attachment that came with it.
- The **Chats** anchor (C) lists every chat the account can see, with its
  **ChatId**. This is where you get the id of a group, which is otherwise
  impossible to find.

Attach a Browse tool to each anchor and run. If you have just linked, send
yourself a message from another phone first so there is something to collect.

> Nothing returned? That is normal on a brand-new link with no new messages.
> WhatsApp gives a newly linked device the messages that arrive *from now on*,
> not your history. Send a test message and run again.

Full settings and column reference: [WhatsApp Input](03-input-tool.md).

---

## 5. Send your first message

1. Drag a **Text Input** tool onto the canvas and give it two columns:

   | Phone | Text |
   | --- | --- |
   | +1 555 0100 | Hello from Alteryx |

2. Connect it to a **WhatsApp Output** tool.
3. In the Output tool:
   - **Profile**: `default` — the same profile you linked.
   - **Send to**: *Take it from a column* → `Phone`.
   - **Message**: *Take it from a column* → `Text`.
4. Run.

The **Results** anchor returns one row per input row, with `Success`, the
`MessageId` WhatsApp assigned, and an `Error` explaining any failure.

Destinations can be a phone number in any format, a chat id from the Input
tool's Chats anchor, or the name of a chat — see
[WhatsApp Output](04-output-tool.md).

---

## 6. A useful pattern: auto-reply

The two tools compose into something genuinely useful in about five minutes:

```
WhatsApp Input ──► Filter ──► Formula ──► WhatsApp Output
   (Messages)     (Body      (build the    (Send to = ChatId)
                  contains   reply text)
                  "status")
```

- **WhatsApp Input** with **Only messages not returned by a previous run**
  ticked, so each run handles only what is new.
- **Filter** on `Body` to pick the messages you care about.
- **Formula** to build the reply.
- **WhatsApp Output** with **Send to** pointed at the `ChatId` column — replies
  go back to whichever chat the message came from, group or direct.

Schedule that workflow every few minutes and you have a working WhatsApp bot
with no server.

---

## Where things are stored

Each profile keeps its linked device and its message archive under:

```
%LOCALAPPDATA%\Alteryx\WhatsAppConnector\profiles\<profile>\
    session.sqlite3     the linked device (treat as a credential)
    archive.sqlite3     collected messages and the chat directory
    media\              downloaded attachments
    profile.json        which account is linked
```

You can point both tools at a different folder with **Advanced → Store sessions
and archives in**. Use the same folder in both, or they will not share a link.

---

## Next

- [Linking a device](02-linking-a-device.md) — profiles, re-linking, several accounts
- [WhatsApp Input](03-input-tool.md) — every setting and column
- [WhatsApp Output](04-output-tool.md) — attachments, pacing, error routing
- [Limits and legal](08-limits-and-legal.md) — read before production
