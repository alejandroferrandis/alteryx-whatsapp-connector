"""Static checks for the configuration panels.

The panels are the one part of this connector with no unit tests, because they
run inside Designer's CEF host against an SDK that only exists there. That gap
cost real time: an unverified data-item type threw during ``BeforeLoad``, the
SDK stopped rendering, and the tool showed a completely blank configuration
window with no error anywhere.

This script closes as much of that gap as can be closed from outside Designer:

1. **Type validation.** Every ``kind`` passed to ``AlteryxDataItems`` and every
   widget ``type`` in the markup must appear in the SDK bundle Designer ships.
   A typo or an invented type is caught here instead of at a customer's desk.
2. **Binding validation.** Every data item must bind to a ``widgetId`` that
   actually exists in the markup, and every widget should have an item.
3. **Lifecycle harness.** Writes an HTML page that executes each panel's
   ``BeforeLoad`` and ``AfterLoad`` against stubbed SDK objects, so syntax
   errors and logic faults surface in a browser.

    python tools/check_panels.py            # checks 1 and 2
    python tools/check_panels.py --harness  # also write the harness page

What it cannot prove is that a *real* SDK data item accepts the options given
to it - the stubs are not the SDK. That is why both panels wrap their lifecycle
hooks in try/catch and render a visible banner: whatever slips through here
should never again show up as a blank window.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui"

PANELS = {
    "Input": UI / "WhatsAppInput" / "WhatsAppInputGui.html",
    "Output": UI / "WhatsAppOutput" / "WhatsAppOutputGui.html",
}

#: Kinds this project synthesises in BeforeLoad rather than passing straight to
#: the SDK. See the ColumnSelector branch in the Output panel.
SYNTHETIC_KINDS = {"ColumnSelector"}


def sdk_bundle() -> Path | None:
    base = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    candidate = (
        base / "Alteryx" / "bin" / "RuntimeData" / "HtmlAssets" / "Shared"
        / "2" / "lib" / "build" / "designerDesktop.bundle.js"
    )
    return candidate if candidate.is_file() else None


def known_types(bundle: Path) -> tuple[set[str], set[str]]:
    """(data item kinds, widget types) that Designer's SDK registers."""
    text = bundle.read_text(encoding="utf-8", errors="replace")

    kinds: set[str] = set(re.findall(r"\b(Simple[A-Za-z]+)\b", text))
    lookup = re.search(r"FieldSelector:[A-Za-z0-9_$.]+,(?:[A-Za-z]+:[A-Za-z0-9_$.]+,?)+", text)
    if lookup:
        kinds |= set(re.findall(r"([A-Za-z][A-Za-z0-9]*)\s*:", lookup.group(0)))

    widgets: set[str] = set()
    registry = re.search(r"PluginWidgetLookup=\{([^}]*)\}", text)
    if registry:
        widgets = set(re.findall(r"([A-Za-z][A-Za-z0-9]*)\s*:", registry.group(1)))

    return kinds, widgets


def panel_parts(path: Path) -> dict:
    source = path.read_text(encoding="utf-8")
    # The panel's logic is the LAST script block; the first only does the
    # document.write that pulls in the SDK.
    scripts = re.findall(r'<script type="text/javascript">(.*?)</script>', source, re.S)
    body = re.search(r"<body>(.*?)<script", source, re.S)
    return {
        "js": scripts[-1] if scripts else "",
        "html": body.group(1) if body else "",
        "kinds": set(re.findall(r"kind:\s*'([A-Za-z]+)'", source)),
        "widget_types": set(re.findall(r'type:"([A-Za-z]+)"', source)),
        "widget_ids": set(re.findall(r'widgetId:"([A-Za-z0-9_]+)"', source)),
        "bound_ids": set(re.findall(r"widget:\s*'([A-Za-z0-9_]+)'", source)),
    }


def check() -> int:
    bundle = sdk_bundle()
    if bundle is None:
        print("Designer's SDK bundle was not found; skipping type validation.")
        known_kinds, known_widgets = set(), set()
    else:
        known_kinds, known_widgets = known_types(bundle)
        print(f"SDK bundle: {bundle}")
        print(f"  {len(known_kinds)} data item kinds, {len(known_widgets)} widget types\n")

    failures = 0
    for name, path in PANELS.items():
        parts = panel_parts(path)
        print(f"{name} panel  ({path.name})")

        used_kinds = parts["kinds"] - SYNTHETIC_KINDS
        if known_kinds:
            unknown = sorted(used_kinds - known_kinds)
            print(f"  data item kinds : {', '.join(sorted(used_kinds))}")
            if unknown:
                print(f"  ERROR unknown kinds: {', '.join(unknown)}")
                failures += 1

        if known_widgets:
            unknown = sorted(parts["widget_types"] - known_widgets)
            print(f"  widget types    : {', '.join(sorted(parts['widget_types']))}")
            if unknown:
                print(f"  ERROR unknown widget types: {', '.join(unknown)}")
                failures += 1

        # A data item bound to a widget that is not in the markup silently does
        # nothing, and the setting appears to be ignored at runtime.
        dangling = sorted(parts["bound_ids"] - parts["widget_ids"])
        if dangling:
            print(f"  ERROR bound to missing widgets: {', '.join(dangling)}")
            failures += 1

        orphans = sorted(parts["widget_ids"] - parts["bound_ids"])
        if orphans:
            print(f"  WARNING widgets with no data item: {', '.join(orphans)}")

        if not dangling:
            print(f"  bindings        : {len(parts['bound_ids'])} ok")
        print()

    print("FAIL" if failures else "OK - every type and binding checks out")
    return 1 if failures else 0


HARNESS = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Panel harness</title>
<style>body{font-family:system-ui;margin:20px;background:#111;color:#eee}
.ok{color:#4ade80}.bad{color:#f87171}h2{border-bottom:1px solid #444;padding-bottom:4px}</style>
</head><body><h1>Alteryx panel lifecycle harness</h1><div id="out"></div>
<div id="sandbox" style="display:none"></div>
<script>
const PANELS = __BLOBS__, KINDS = __KINDS__;
const out = document.getElementById('out');
const log = (c, m) => { const d = document.createElement('div'); d.className = c; d.textContent = m; out.appendChild(d); };

function makeItem(name, options){
  let value = ''; const ls = [];
  return { name, options: options || {}, getValue: () => value,
           setValue: v => { value = v; ls.forEach(f => f()); },
           registerPropertyListener: (_p, f) => ls.push(f) };
}
// Only kinds the real SDK registers may be constructed.
const AlteryxDataItems = new Proxy({}, { get: (_t, kind) => {
  if (typeof kind !== 'string') return undefined;
  if (KINDS.length && KINDS.indexOf(kind) === -1) {
    return function(){ throw new Error('data item kind not in the SDK: ' + kind); };
  }
  return function(name, options){ return makeItem(name, options); };
}});

function makeManager(fields){
  const items = {};
  return { addDataItem: i => { items[i.name] = i; },
           bindDataItemToWidget: (i, w) => { if (!i || !w) throw new Error('bad bind'); },
           getDataItem: n => { if (!items[n]) throw new Error('unknown data item "' + n + '"'); return items[n]; },
           getIncomingFields: () => fields, _items: items };
}

for (const [name, panel] of Object.entries(PANELS)) {
  const h = document.createElement('h2'); h.textContent = name + ' panel'; out.appendChild(h);
  document.getElementById('sandbox').innerHTML = panel.html;
  window.Alteryx = { LibDir: '', Gui: {} };
  try { (0, eval)(panel.js); log('ok', 'script parsed'); }
  catch (e) { log('bad', 'SYNTAX ERROR: ' + e.message); continue; }

  const cases = [['3 incoming columns', [{name:'Phone'},{name:'Text'},{name:'FilePath'}], {}],
                 ['no incoming columns', [], {}],
                 ['saved config, stale column', [{name:'Other'}],
                  {ToField:{'@value':'Phone'}, Profile:{'@value':'default'}}]];
  for (const [label, fields, config] of cases) {
    const mgr = makeManager(fields);
    try { window.Alteryx.Gui.BeforeLoad(mgr, AlteryxDataItems, {Configuration: config});
          log('ok', 'BeforeLoad ok (' + label + ') - ' + Object.keys(mgr._items).length + ' items'); }
    catch (e) { log('bad', 'BeforeLoad FAILED (' + label + '): ' + e.message); continue; }
    try { window.Alteryx.Gui.AfterLoad(mgr); log('ok', 'AfterLoad ok (' + label + ')'); }
    catch (e) { log('bad', 'AfterLoad FAILED (' + label + '): ' + e.message); }
  }
}
log('', '--- done ---');
</script></body></html>"""


def write_harness(target: Path) -> None:
    bundle = sdk_bundle()
    kinds = sorted(known_types(bundle)[0]) if bundle else []
    blobs = {
        name: {"js": p["js"], "html": p["html"]}
        for name, p in ((n, panel_parts(path)) for n, path in PANELS.items())
    }
    page = HARNESS.replace("__BLOBS__", json.dumps(blobs)).replace("__KINDS__", json.dumps(kinds))
    # build/ is gitignored, so it does not exist on a fresh checkout - which is
    # exactly when someone is most likely to try this.
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page, encoding="utf-8")
    print(f"\nHarness written to {target}")
    print("Serve it and open it in a browser - file:// will not execute it:")
    print(f'  python -m http.server 8731 --bind 127.0.0.1 --directory "{target.parent}"')
    print("  then open http://127.0.0.1:8731/" + target.name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--harness", action="store_true",
                        help="Also write the browser lifecycle harness")
    parser.add_argument("--out", default="", help="Where to write the harness")
    args = parser.parse_args()

    status = check()
    if args.harness:
        write_harness(Path(args.out) if args.out else ROOT / "build" / "panel_harness.html")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
