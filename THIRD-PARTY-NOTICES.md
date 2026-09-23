# Third-party notices

The WhatsApp Connector for Alteryx Designer bundles the components below inside
its `.yxi`. Their licences are reproduced or referenced here, and each package's
own licence text ships alongside it in `site-packages/<package>.dist-info/`.

This file covers the **bundled dependencies**. The connector's own code is
MIT-licensed — see `LICENSE`.

---

## whatsmeow — Mozilla Public License 2.0

**This is the one that carries an obligation on anyone who redistributes the
bundled `.yxi`.**

The connector bundles `neonize-windows-amd64.dll`: a compiled build of
[whatsmeow](https://github.com/tulir/whatsmeow), a Go implementation of the
WhatsApp multi-device protocol, by Tulir Asokan. It is licensed under the
**Mozilla Public License, version 2.0**.

MPL-2.0 is a file-level copyleft licence. In plain terms, as it applies here:

- The source of whatsmeow is available at <https://github.com/tulir/whatsmeow>.
- This project does not modify whatsmeow. If a future version does, those
  modifications must be published under MPL-2.0.
- Bundling it inside a larger work is permitted, and MPL-2.0 does not reach
  the connector's own code - which is why this project can be MIT while the
  DLL inside it stays MPL-2.0.
- A copy of the licence must accompany the distribution. It is included below
  by reference and in full at <https://mozilla.org/MPL/2.0/>.

> This Source Code Form is subject to the terms of the Mozilla Public License,
> v. 2.0. If a copy of the MPL was not distributed with this file, You can
> obtain one at <https://mozilla.org/MPL/2.0/>.

The compiled library is produced and published by the
[neonize](https://github.com/krypton-byte/neonize) project, which wraps
whatsmeow for Python; neonize itself is Apache-2.0.

---

## Alteryx Python SDK — Alteryx SDK and API License Agreement

`ayx-python-sdk` is distributed under the
[Alteryx SDK and API License Agreement](https://www.alteryx.com/alteryx-sdk-and-api-license-agreement),
which permits bundling it inside a tool built on the SDK. That agreement governs
its use; review it before redistributing the bundled `.yxi`.

---

## Every bundled distribution

45 Python distributions ship inside the `.yxi`, listed here in full
rather than summarised. Versions are those of this release; each package's own
licence text is in `site-packages/<name>.dist-info/`.

Generated from the package itself, not maintained by hand.

| Distribution | Version | Licence |
| --- | --- | --- |
| `annotated-types` | 0.8.0 | MIT |
| `anyio` | 4.15.1 | MIT |
| `ayx_python_sdk` | 2.6.1 | Alteryx SDK and API License Agreement |
| `beautifulsoup4` | 4.15.0 | MIT |
| `certifi` | 2026.7.22 | MPL-2.0 |
| `charset-normalizer` | 3.5.1 | MIT |
| `click` | 8.1.7 | BSD-3-Clause |
| `colorama` | 0.4.6 | BSD-3-Clause |
| `deprecation` | 2.1.0 | Apache-2.0 |
| `dukpy` | 0.6.0 | MIT |
| `filelock` | 4.0.1 | MIT |
| `grpcio-fips` | 1.81.1 | Apache-2.0 |
| `h11` | 0.16.0 | MIT |
| `httpcore` | 1.0.9 | BSD-3-Clause |
| `httpx` | 0.28.1 | BSD-3-Clause |
| `idna` | 3.20 | BSD-3-Clause |
| `linkpreview` | 0.12.1 | MIT |
| `neonize` | 0.5.2 | Apache-2.0 |
| `numpy` | 2.5.3 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| `packaging` | 26.3 | Apache-2.0 OR BSD-2-Clause |
| `pandas` | 2.3.3 | BSD-3-Clause |
| `phonenumbers` | 9.0.39 | Apache-2.0 |
| `pillow` | 12.3.0 | MIT-CMU |
| `protobuf` | 7.34.1 | BSD-3-Clause |
| `psutil` | 7.0.0 | BSD-3-Clause |
| `pyarrow` | 23.0.1 | Apache-2.0 |
| `pydantic` | 2.10.6 | MIT |
| `pydantic_core` | 2.27.2 | MIT |
| `PyPAC` | 0.16.5 | Apache-2.0 |
| `python-dateutil` | 2.8.2 | Apache-2.0 OR BSD-3-Clause |
| `python-magic-bin` | 0.4.14 | MIT |
| `pytz` | 2023.3.post1 | MIT |
| `requests` | 2.34.2 | Apache-2.0 |
| `requests-file` | 3.0.1 | Apache-2.0 |
| `segno` | 1.6.6 | BSD-3-Clause |
| `six` | 1.16.0 | MIT |
| `soupsieve` | 2.9.2 | MIT |
| `tldextract` | 5.3.2 | BSD-3-Clause |
| `tqdm` | 4.70.1 | MPL-2.0 AND MIT |
| `typer` | 0.9.0 | MIT |
| `typing_extensions` | 4.16.0 | PSF-2.0 |
| `tzdata` | 2026.4 | Apache-2.0 |
| `urllib3` | 2.8.0 | MIT |
| `wincertstore` | 0.2 | PSF-2.0 |
| `xmltodict` | 1.0.4 | MIT |

Plus the compiled **whatsmeow** core inside `neonize-windows-amd64.dll`
(MPL-2.0), covered above.

### The copyleft ones

Three bundled components carry MPL-2.0 terms. None of them restrict this
project's MIT licence, because MPL-2.0 is file-level: it reaches only its own
files, and none of them are modified here.

| Component | Why it is here | What MPL-2.0 asks |
| --- | --- | --- |
| **whatsmeow** (in the DLL) | The WhatsApp protocol itself | Source stays available; publish modifications to its files. Unmodified here. |
| **certifi** | Root CA bundle for TLS | Same, unmodified. |
| **tqdm** | Progress reporting (dual MPL-2.0/MIT; used under MIT) | Same, unmodified. |

`dukpy` additionally embeds Duktape (MIT), Babel (MIT) and TypeScript
(Apache-2.0); their notices ship inside that package.

---

## Not bundled

**FFmpeg is not included** and is not required. See
`docs/08-limits-and-legal.md` for why, and for what changes if you install it
yourself.

---

## Trademarks

**WhatsApp** is a trademark of WhatsApp LLC / Meta Platforms, Inc. This product
is not affiliated with, endorsed by or connected to them. **Alteryx** and
**Alteryx Designer** are trademarks of Alteryx, Inc.; this is a third-party
product built on their published SDK.
