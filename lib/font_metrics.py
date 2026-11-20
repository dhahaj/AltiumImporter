"""
Vertical metrics of installed TrueType/OpenType fonts (Windows), used to
convert Altium's TrueType text height into the cap height Fusion expects.

Altium sizes TrueType text by the font's full cell height
(usWinAscent + usWinDescent); Fusion's sketch text height is the cap height.
Pure Python: reads the OS/2 and head tables directly. Falls back to typical
values when the font file can't be found (e.g. not installed on this PC).
"""

import os
import struct

# cap height / cell height and descent / cell height for a typical sans font
DEFAULT = (0.60, 0.19)
_cache = {}
_index = None


def _font_index():
    """{lower-case registry name without '(TrueType)': file path}"""
    global _index
    if _index is not None:
        return _index
    _index = {}
    try:
        import winreg
    except ImportError:
        return _index
    windir = os.environ.get("WINDIR", r"C:\Windows")
    key = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            k = winreg.OpenKey(hive, key)
        except OSError:
            continue
        i = 0
        while True:
            try:
                name, val, _ = winreg.EnumValue(k, i)
            except OSError:
                break
            i += 1
            if not isinstance(val, str):
                continue
            path = val if os.path.isabs(val) else os.path.join(windir, "Fonts", val)
            base = name.split(" (")[0].strip().lower()
            for part in base.split(" & "):        # TTC entries: "Cambria & Cambria Math"
                _index.setdefault(part.strip(), path)
    return _index


def _find(family, bold, italic):
    idx = _font_index()
    fam = family.strip().lower()
    style = ("bold italic" if bold and italic else "bold" if bold
             else "italic" if italic else "")
    for cand in ([fam + " " + style] if style else []) + [fam, fam + " regular"]:
        if cand in idx:
            return idx[cand]
    for name, path in idx.items():
        if name.startswith(fam + " "):
            return path
    return None


def _read_metrics(path):
    with open(path, "rb") as f:
        data = f.read()
    off = 0
    if data[:4] == b"ttcf":
        off = struct.unpack(">I", data[12:16])[0]
    num = struct.unpack(">H", data[off + 4:off + 6])[0]
    tables = {}
    for i in range(num):
        rec = off + 12 + 16 * i
        tag = data[rec:rec + 4]
        tables[tag] = struct.unpack(">II", data[rec + 8:rec + 16])
    head = tables[b"head"][0]
    upem = struct.unpack(">H", data[head + 18:head + 20])[0]
    os2 = tables[b"OS/2"][0]
    version = struct.unpack(">H", data[os2:os2 + 2])[0]
    win_asc, win_desc = struct.unpack(">HH", data[os2 + 74:os2 + 78])
    cap = None
    if version >= 2:
        cap = struct.unpack(">h", data[os2 + 88:os2 + 90])[0]
    if not cap or cap <= 0:
        cap = 0.70 * upem
    cell = float(win_asc + win_desc) or 1.2 * upem
    return cap / cell, win_desc / cell


def metrics(family, bold=False, italic=False):
    """(cap_ratio, descent_ratio) relative to Altium's TrueType text height."""
    key = (family.lower(), bold, italic)
    if key not in _cache:
        res = DEFAULT
        try:
            path = _find(family, bold, italic)
            if path and os.path.exists(path):
                cap, desc = _read_metrics(path)
                if 0.3 < cap < 0.9 and 0.0 <= desc < 0.5:
                    res = (cap, desc)
        except Exception:
            res = DEFAULT
        _cache[key] = res
    return _cache[key]
