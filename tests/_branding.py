"""Loader for the attribution word list used by the branding checks.

The list itself lives in `tests\\branding_words.py`, which is deliberately NOT
part of the public repository: publishing the words would defeat the rule they
exist to enforce. When the file is absent — every clone — `words()` returns
empty tuples and the checks that use it pass trivially instead of failing a
checkout that could never satisfy them.

    forbidden, studied = _branding.words(_ROOT)
"""
import importlib.util
import os


def words(root):
    """(forbidden, studied) from the local-only list, or two empty tuples.

    `forbidden` must appear nowhere in shipped UI text, comments or docs.
    `studied` names add-ons whose behaviour was researched during development;
    the engines we ship must not name them either.
    """
    path = os.path.join(root, "tests", "branding_words.py")
    if not os.path.exists(path):
        return (), ()
    spec = importlib.util.spec_from_file_location("_madi_branding_words", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return tuple(getattr(mod, "FORBIDDEN", ())), tuple(getattr(mod, "STUDIED", ()))
