# Third-party engine — QuadWild-BiMDF

The two executables in this folder are **not part of the MadihsonNSFW Toolset**.
They are unmodified upstream binaries, redistributed here so that the Quadify
tool works without a separate download.

| | |
|---|---|
| **Project** | QuadWild-BiMDF |
| **Upstream** | <https://github.com/cgg-bern/quadwild-bimdf/> |
| **Version** | 0.0.2 (binary release, 28 September 2023) |
| **Files** | `quadwild.exe`, `quad_from_patches.exe`, `config/` |
| **Licence** | **GPL-3.0** |
| **Method** | QuadWild + Bi-MDF, published at SIGGRAPH 2021 and 2023 |

We do not write the remeshing maths. The MADI Quadify tool prepares a mesh,
runs these programs as a **subprocess**, and reads the result back — see
`../quadify.py` for why it is a subprocess and never loaded into Blender.

## Licence

These binaries are licensed under the GNU General Public License, version 3 —
the same licence as this repository. The full text is in
[`LICENSE`](../../../LICENSE) at the root of this project.

**Corresponding source** for these binaries is published by the upstream
project at the URL above, where the release they came from is also available.

## Citing the work

If you use the remeshing results in published work, cite the upstream authors
rather than this project — the papers and the preferred citation are listed on
the upstream repository.

## Removing them

Nothing else in the Toolset depends on these files. Delete this folder and
every other tool keeps working; Quadify reports that its engine is missing and
does nothing else.
