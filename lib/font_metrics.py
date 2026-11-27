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


def _style_names(bold, italic):
    return ("bold italic" if bold and italic else "bold" if bold
            else "italic" if italic else "")


def _find(family, bold, italic):
    """(path, styled): the font file for family, and whether it is the
    requested bold/italic face (False when only the regular face exists)."""
    idx = _font_index()
    fam = family.strip().lower()
    style = _style_names(bold, italic)
    if style and fam + " " + style in idx:
        return idx[fam + " " + style], True
    for cand in (fam, fam + " regular"):
        if cand in idx:
            return idx[cand], not style
    # The registry name is often not the family name ("ROGFonts-Regular" for
    # family "ROG Fonts"), so ask the font files themselves.
    faces = _scan_families().get(fam)
    if faces:
        want = style or "regular"
        for sub, path in faces:
            if sub == want:
                return path, True
        for sub, path in faces:
            if sub in ("regular", "normal", "book", "roman"):
                return path, not style
        return faces[0][1], False
    for name, path in idx.items():
        if name.startswith(fam + " "):
            return path, not style
    return None, False


_families = None


def _scan_families():
    """{lower-case family name: [(lower-case subfamily, file path)]} from the
    fonts' own name tables, for families the registry names don't give
    away. Reads only each file's table directory and name table."""
    global _families
    if _families is None:
        _families = {}
        try:
            import font_outline
        except ImportError:
            return _families
        for path in sorted(set(_font_index().values())):
            try:
                pairs = font_outline.file_families(path, with_style=True)
            except Exception:
                continue
            for fam, sub in pairs:
                faces = _families.setdefault(fam.lower(), [])
                if (sub.lower(), path) not in faces:
                    faces.append((sub.lower(), path))
    return _families


def font_file(family, bold=False, italic=False):
    """(path or None, styled) for an installed font family; styled is False
    when the bold/italic face asked for isn't installed."""
    try:
        path, styled = _find(family, bold, italic)
    except Exception:
        return None, False
    if path and os.path.exists(path):
        return path, styled
    return None, False


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
            path, _ = font_file(family, bold, italic)
            if path:
                cap, desc = _read_metrics(path)
                if 0.3 < cap < 0.9 and 0.0 <= desc < 0.5:
                    res = (cap, desc)
        except Exception:
            res = DEFAULT
        _cache[key] = res
    return _cache[key]
