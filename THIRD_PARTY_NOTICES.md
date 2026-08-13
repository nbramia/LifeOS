# Third-Party Notices

LifeOS itself is licensed under the GNU General Public License v3.0 — see [LICENSE](LICENSE).

This file lists third-party material redistributed as part of this repository, along with its
origin and license. Redistributing that material carries its own attribution obligations, which
this file exists to satisfy.

---

## `config/nicknames.csv` — English given-name / nickname dataset

| | |
|---|---|
| **Upstream** | https://github.com/carltonnorthern/nicknames |
| **Upstream file** | `names.csv` |
| **License** | Apache License 2.0 — full text at [licenses/Apache-2.0.txt](licenses/Apache-2.0.txt) |
| **Copyright** | The `nicknames` project contributors. The dataset originated with Old Dominion University's Web Science and Digital Libraries Research Group and is maintained by [@carltonnorthern](https://github.com/carltonnorthern). |
| **Used by** | [`config/nickname_lookup.py`](config/nickname_lookup.py) — bidirectional formal-name ↔ nickname lookup during entity resolution |

**Modifications:** the file was renamed from `names.csv` to `nicknames.csv`. The contents are a
vendored snapshot of an earlier upstream revision — no rows have been added, removed, or edited
by this project, and the `name1,relationship,name2` schema is unchanged. As of the last sync check
the snapshot trails upstream by 136 rows, all of them upstream additions.

Apache-2.0 and GPL-3.0 are compatible in this direction: Apache-2.0 material may be included in a
GPL-3.0 work. The combined work is distributed under GPL-3.0, while this component remains under
Apache-2.0 as recorded above.

---

## Adding to this file

If you vendor third-party code or data into the repository, add a section here recording the
upstream URL, license, copyright holder, where it is used, and any modifications you made. If the
license is one not already present, add its full text under `licenses/`.
