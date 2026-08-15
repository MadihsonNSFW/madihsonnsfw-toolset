"""Build `docs\\PITFALLS.md` — every ⚠ in the project, on one page.

    python tools\\gen_pitfalls.py

WHY THIS EXISTS
There are ~400 ⚠ blocks across the docs and they are the most valuable thing
in the repo: each one is a decision that somebody will otherwise re-break,
usually by "simplifying" it. Spread across eighteen files they are only found
by whoever happens to open the right doc. On one page they can be skimmed in a
couple of minutes before touching an unfamiliar module.

⚠ **The index is GENERATED, and `tests\\docs_test.py` fails when it is stale.**
A hand-maintained index of warnings is exactly the thing that quietly stops
matching what it indexes — the same reasoning as the add-on advertising its
capabilities from its own dispatcher source rather than a hand-written list.
Run this after editing any doc; it takes a moment.
"""
import io
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
OUT = os.path.join(DOCS, "PITFALLS.md")
OUT_NAME = "PITFALLS.md"

HEADER = """# ⚠ PITFALLS — every warning in the project, on one page
**GENERATED — do not edit.** Run `python tools\\gen_pitfalls.py` after changing
any doc; `tests\\docs_test.py` fails on a stale index.

Each line is a decision that cost something to learn. **Skim the section for a
module before you touch it**; follow the file reference for the reasoning,
because the one-line form here is a reminder, not an explanation.

"""

_MD = re.compile(r"[*`_]+")


def headline(text):
    """The ⚠ line, flattened to something scannable."""
    text = text.strip().lstrip("-").strip()
    text = text[text.index("⚠"):] if "⚠" in text else text
    text = text.lstrip("⚠").strip()
    text = _MD.sub("", text)
    text = re.sub(r"\s+", " ", text)
    return text


def scan(path):
    out = []
    with io.open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if "⚠" not in line:
                continue
            text = headline(line)
            if not text:
                # a bare ⚠ continued on the next line — keep the position, the
                # reader is going to open the file anyway
                text = "(see the file)"
            out.append((n, text))
    return out


def build():
    names = sorted(n for n in os.listdir(DOCS)
                   if n.endswith(".md") and n != OUT_NAME)
    # HANDOFF last: it is a router, its warnings point elsewhere
    sources = [(n, os.path.join(DOCS, n)) for n in names]
    sources.append(("..\\HANDOFF.md", os.path.join(ROOT, "HANDOFF.md")))

    parts = [HEADER]
    total = 0
    for label, path in sources:
        if not os.path.isfile(path):
            continue
        found = scan(path)
        if not found:
            continue
        total += len(found)
        parts.append("## %s  (%d)\n" % (label, len(found)))
        for n, text in found:
            parts.append("- **L%d** — %s" % (n, text))
        parts.append("")
    parts.insert(1, "**%d warnings across %d files.**\n"
                 % (total, sum(1 for _l, p in sources if os.path.isfile(p))))
    return "\n".join(parts).rstrip() + "\n"


def main():
    text = build()
    current = ""
    if os.path.isfile(OUT):
        with io.open(OUT, encoding="utf-8") as fh:
            current = fh.read()
    if current == text:
        print("PITFALLS.md already up to date")
        return 0
    with io.open(OUT, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    print("wrote %s (%d lines)" % (OUT, len(text.split("\n"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
