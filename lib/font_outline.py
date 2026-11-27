"""
Glyph outlines straight from an installed font file, for fonts Fusion can
type in a sketch but cannot turn into profiles (many PostScript-flavoured
.otf fonts, e.g. "ROG Fonts": the sketch text appears, the extrude fails
with "Some input argument is invalid").

Pure Python, no dependencies. Reads TrueType (glyf) and OpenType CFF
outlines, cmap, hmtx and the legacy kern table, and returns each glyph as
closed polylines in font units (y up, baseline at 0).
"""

import math
import struct

_fonts = {}


def load(path):
    """Cached Font for path (raises on files it can't read)."""
    f = _fonts.get(path)
    if f is None:
        f = _fonts[path] = Font(path)
    return f


class FontError(Exception):
    pass


# -- curve flattening ----------------------------------------------------------
def _quad(out, p0, p1, p2, tol):
    d = math.hypot(p0[0] - 2 * p1[0] + p2[0], p0[1] - 2 * p1[1] + p2[1]) / 4
    n = max(1, min(32, int(math.ceil(math.sqrt(d / tol)))))
    for i in range(1, n + 1):
        t = i / n
        a, b, c = (1 - t) ** 2, 2 * t * (1 - t), t * t
        out.append((a * p0[0] + b * p1[0] + c * p2[0], a * p0[1] + b * p1[1] + c * p2[1]))


def _cubic(out, p0, p1, p2, p3, tol):
    d = max(math.hypot(p0[0] - 2 * p1[0] + p2[0], p0[1] - 2 * p1[1] + p2[1]),
            math.hypot(p1[0] - 2 * p2[0] + p3[0], p1[1] - 2 * p2[1] + p3[1])) * 0.75
    n = max(1, min(48, int(math.ceil(math.sqrt(d / tol)))))
    for i in range(1, n + 1):
        t = i / n
        u = 1 - t
        a, b, c, e = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
        out.append((a * p0[0] + b * p1[0] + c * p2[0] + e * p3[0],
                    a * p0[1] + b * p1[1] + c * p2[1] + e * p3[1]))


def _close(pts):
    """Drop the repeated closing point and zero-length steps."""
    out = []
    for p in pts:
        if not out or abs(p[0] - out[-1][0]) > 1e-9 or abs(p[1] - out[-1][1]) > 1e-9:
            out.append(p)
    while len(out) > 1 and abs(out[0][0] - out[-1][0]) < 1e-9 and abs(out[0][1] - out[-1][1]) < 1e-9:
        out.pop()
    return out if len(out) >= 3 else None


def area(loop):
    """Signed area (positive = counter-clockwise)."""
    s = 0.0
    n = len(loop)
    for i in range(n):
        x0, y0 = loop[i]
        x1, y1 = loop[(i + 1) % n]
        s += x0 * y1 - x1 * y0
    return s / 2


# -- CFF ---------------------------------------------------------------------------
def _index(data, pos):
    """(list of byte strings, position after the INDEX)."""
    count = struct.unpack(">H", data[pos:pos + 2])[0]
    if count == 0:
        return [], pos + 2
    osz = data[pos + 2]
    base = pos + 3
    offs = []
    for i in range(count + 1):
        o = 0
        for b in data[base + i * osz: base + (i + 1) * osz]:
            o = (o << 8) | b
        offs.append(o)
    start = base + (count + 1) * osz - 1
    return [data[start + offs[i]:start + offs[i + 1]] for i in range(count)], start + offs[-1]


def _dict(raw):
    d, stack, i = {}, [], 0
    while i < len(raw):
        b = raw[i]
        if b <= 21:
            if b == 12:
                op = 1200 + raw[i + 1]
                i += 2
            else:
                op = b
                i += 1
            d[op] = stack
            stack = []
        elif b == 28:
            stack.append(struct.unpack(">h", raw[i + 1:i + 3])[0])
            i += 3
        elif b == 29:
            stack.append(struct.unpack(">i", raw[i + 1:i + 5])[0])
            i += 5
        elif b == 30:                      # real number, nibbles
            s, i = "", i + 1
            done = False
            while not done:
                byte = raw[i]
                i += 1
                for nib in (byte >> 4, byte & 15):
                    if nib == 15:
                        done = True
                        break
                    s += "0123456789.EE?-"[nib] + ("-" if nib == 12 else "")
            try:
                stack.append(float(s))
            except ValueError:
                stack.append(0.0)
        elif 32 <= b <= 246:
            stack.append(b - 139)
            i += 1
        elif 247 <= b <= 250:
            stack.append((b - 247) * 256 + raw[i + 1] + 108)
            i += 2
        elif 251 <= b <= 254:
            stack.append(-(b - 251) * 256 - raw[i + 1] - 108)
            i += 2
        else:
            i += 1
    return d


def _bias(subrs):
    n = len(subrs)
    return 107 if n < 1240 else 1131 if n < 33900 else 32768


class _CFF:
    def __init__(self, data):
        hdr = data[2]
        _, pos = _index(data, hdr)                  # names
        tops, pos = _index(data, pos)
        _, pos = _index(data, pos)                  # strings
        self.gsubrs, pos = _index(data, pos)
        top = _dict(tops[0])
        if 17 not in top:
            raise FontError("CFF without CharStrings")
        self.charstrings, _ = _index(data, top[17][0])
        self.fd_select = None
        if 1236 in top:                             # CID-keyed: FDArray + FDSelect
            fds, _ = _index(data, top[1236][0])
            self.privates = [self._private(data, _dict(fd)) for fd in fds]
            self.fd_select = self._fdselect(data, top[1237][0], len(self.charstrings))
        else:
            self.privates = [self._private(data, top)]

    @staticmethod
    def _private(data, d):
        if 18 not in d:
            return []
        size, off = d[18][0], d[18][1]
        p = _dict(data[off:off + size])
        if 19 in p:
            subrs, _ = _index(data, off + p[19][0])
            return subrs
        return []

    @staticmethod
    def _fdselect(data, pos, n):
        fmt = data[pos]
        if fmt == 0:
            return list(data[pos + 1:pos + 1 + n])
        sel = [0] * n
        nr = struct.unpack(">H", data[pos + 1:pos + 3])[0]
        for r in range(nr):
            first, fd = struct.unpack(">HB", data[pos + 3 + 3 * r:pos + 6 + 3 * r])
            nxt = struct.unpack(">H", data[pos + 6 + 3 * r:pos + 8 + 3 * r])[0]
            for g in range(first, min(nxt, n)):
                sel[g] = fd
        return sel

    def glyph(self, gid, tol):
        """Closed polylines for glyph gid."""
        if gid >= len(self.charstrings):
            return []
        subrs = self.privates[self.fd_select[gid] if self.fd_select else 0]
        st = {"stack": [], "nstems": 0, "x": 0.0, "y": 0.0, "width": False,
              "contours": [], "cur": None, "done": False}
        self._run(self.charstrings[gid], st, subrs, tol, 0)
        self._closepath(st)
        return st["contours"]

    @staticmethod
    def _closepath(st):
        if st["cur"]:
            c = _close(st["cur"])
            if c:
                st["contours"].append(c)
        st["cur"] = None

    def _run(self, cs, st, subrs, tol, depth):
        if depth > 10:
            raise FontError("CFF subroutines nest too deep")
        s = st["stack"]
        i, n = 0, len(cs)

        def move(dx, dy):
            self._closepath(st)
            st["x"] += dx
            st["y"] += dy
            st["cur"] = [(st["x"], st["y"])]

        def line(dx, dy):
            st["x"] += dx
            st["y"] += dy
            if st["cur"] is None:
                st["cur"] = [(st["x"] - dx, st["y"] - dy)]
            st["cur"].append((st["x"], st["y"]))

        def curve(d1x, d1y, d2x, d2y, d3x, d3y):
            p0 = (st["x"], st["y"])
            p1 = (p0[0] + d1x, p0[1] + d1y)
            p2 = (p1[0] + d2x, p1[1] + d2y)
            p3 = (p2[0] + d3x, p2[1] + d3y)
            if st["cur"] is None:
                st["cur"] = [p0]
            _cubic(st["cur"], p0, p1, p2, p3, tol)
            st["x"], st["y"] = p3

        def strip_width(even):
            # the first stack-clearing operator may carry the advance width
            if not st["width"]:
                st["width"] = True
                if (len(s) % 2 == 1) == even:
                    del s[0]

        while i < n and not st["done"]:
            b = cs[i]
            if b >= 32 or b == 28:
                if b == 28:
                    s.append(struct.unpack(">h", cs[i + 1:i + 3])[0])
                    i += 3
                elif b <= 246:
                    s.append(b - 139)
                    i += 1
                elif b <= 250:
                    s.append((b - 247) * 256 + cs[i + 1] + 108)
                    i += 2
                elif b <= 254:
                    s.append(-(b - 251) * 256 - cs[i + 1] - 108)
                    i += 2
                else:
                    s.append(struct.unpack(">i", cs[i + 1:i + 5])[0] / 65536.0)
                    i += 5
                continue
            i += 1
            if b in (1, 3, 18, 23):                 # hstem vstem hstemhm vstemhm
                strip_width(True)
                st["nstems"] += len(s) // 2
                del s[:]
            elif b in (19, 20):                     # hintmask cntrmask
                strip_width(True)
                st["nstems"] += len(s) // 2
                del s[:]
                i += (st["nstems"] + 7) // 8
            elif b == 21:                           # rmoveto
                strip_width(True)
                move(s[-2], s[-1])
                del s[:]
            elif b == 22:                           # hmoveto
                strip_width(False)
                move(s[-1], 0)
                del s[:]
            elif b == 4:                            # vmoveto
                strip_width(False)
                move(0, s[-1])
                del s[:]
            elif b == 5:                            # rlineto
                for k in range(0, len(s) - 1, 2):
                    line(s[k], s[k + 1])
                del s[:]
            elif b in (6, 7):                       # hlineto vlineto
                horiz = b == 6
                for v in s:
                    line(v, 0) if horiz else line(0, v)
                    horiz = not horiz
                del s[:]
            elif b == 8:                            # rrcurveto
                for k in range(0, len(s) - 5, 6):
                    curve(*s[k:k + 6])
                del s[:]
            elif b == 24:                           # rcurveline
                k = 0
                while k + 6 <= len(s) - 2:
                    curve(*s[k:k + 6])
                    k += 6
                line(s[k], s[k + 1])
                del s[:]
            elif b == 25:                           # rlinecurve
                k = 0
                while k + 2 <= len(s) - 6:
                    line(s[k], s[k + 1])
                    k += 2
                curve(*s[k:k + 6])
                del s[:]
            elif b == 26:                           # vvcurveto
                k, dx1 = 0, 0
                if len(s) % 4 == 1:
                    dx1, k = s[0], 1
                while k + 4 <= len(s):
                    curve(dx1, s[k], s[k + 1], s[k + 2], 0, s[k + 3])
                    dx1 = 0
                    k += 4
                del s[:]
            elif b == 27:                           # hhcurveto
                k, dy1 = 0, 0
                if len(s) % 4 == 1:
                    dy1, k = s[0], 1
                while k + 4 <= len(s):
                    curve(s[k], dy1, s[k + 1], s[k + 2], s[k + 3], 0)
                    dy1 = 0
                    k += 4
                del s[:]
            elif b in (30, 31):                     # vhcurveto hvcurveto
                horiz = b == 31
                k = 0
                while k + 4 <= len(s):
                    last = len(s) - k == 5
                    e = s[k + 4] if last else 0
                    if horiz:
                        curve(s[k], 0, s[k + 1], s[k + 2], e, s[k + 3])
                    else:
                        curve(0, s[k], s[k + 1], s[k + 2], s[k + 3], e)
                    horiz = not horiz
                    k += 5 if last else 4
                del s[:]
            elif b == 10 or b == 29:                # callsubr callgsubr
                lst = subrs if b == 10 else self.gsubrs
                idx = int(s.pop()) + _bias(lst)
                self._run(lst[idx], st, subrs, tol, depth + 1)
                s = st["stack"]
            elif b == 11:                           # return
                return
            elif b == 14:                           # endchar
                if not st["width"]:
                    st["width"] = True          # (a width is the odd arg out)
                del s[:]
                self._closepath(st)
                st["done"] = True
                return
            elif b == 12:
                op = cs[i]
                i += 1
                if op == 35:                        # flex
                    curve(*s[0:6])
                    curve(*s[6:12])
                elif op == 34:                      # hflex
                    dx1, dx2, dy2, dx3, dx4, dx5, dx6 = s[:7]
                    curve(dx1, 0, dx2, dy2, dx3, 0)
                    curve(dx4, 0, dx5, -dy2, dx6, 0)
                elif op == 36:                      # hflex1
                    dx1, dy1, dx2, dy2, dx3, dx4, dx5, dy5, dx6 = s[:9]
                    curve(dx1, dy1, dx2, dy2, dx3, 0)
                    curve(dx4, 0, dx5, dy5, dx6, -(dy1 + dy2 + dy5))
                elif op == 37:                      # flex1
                    d = s[:11]
                    dx = d[0] + d[2] + d[4] + d[6] + d[8]
                    dy = d[1] + d[3] + d[5] + d[7] + d[9]
                    curve(*d[0:6])
                    if abs(dx) > abs(dy):
                        curve(d[6], d[7], d[8], d[9], d[10], -dy)
                    else:
                        curve(d[6], d[7], d[8], d[9], -dx, d[10])
                del s[:]
            else:
                del s[:]                            # unknown / arithmetic: ignore


# -- the font ------------------------------------------------------------------------
class Font:
    def __init__(self, path, family=None):
        with open(path, "rb") as fh:
            data = fh.read()
        self.data = data
        off = 0
        if data[:4] == b"ttcf":
            n = struct.unpack(">I", data[8:12])[0]
            offs = struct.unpack(">%dI" % n, data[12:12 + 4 * n])
            off = offs[0]
            if family:
                for o in offs:
                    if family.lower() in [x.lower() for x in _families(data, o)]:
                        off = o
                        break
        num = struct.unpack(">H", data[off + 4:off + 6])[0]
        self.tables = {}
        for i in range(num):
            rec = off + 12 + 16 * i
            self.tables[data[rec:rec + 4]] = struct.unpack(">II", data[rec + 8:rec + 16])
        head = self._t(b"head")
        self.upem = struct.unpack(">H", head[18:20])[0]
        self.loc_long = struct.unpack(">h", head[50:52])[0] == 1
        os2 = self._t(b"OS/2", required=False)
        hhea = self._t(b"hhea")
        if os2 and len(os2) >= 78:
            asc, desc = struct.unpack(">HH", os2[74:78])
        else:
            asc, desc = struct.unpack(">hh", hhea[4:8])
            desc = -desc
        # Altium's TrueType text height is the whole cell
        self.cell = float(asc + desc) or 1.2 * self.upem
        self.descent = float(desc)
        n_hm = struct.unpack(">H", hhea[34:36])[0]
        hmtx = self._t(b"hmtx")
        self.advances = [struct.unpack(">H", hmtx[4 * i:4 * i + 2])[0] for i in range(n_hm)]
        self.num_glyphs = struct.unpack(">H", self._t(b"maxp")[4:6])[0]
        self.cmap = self._cmap()
        self.kern = self._kern()
        if b"glyf" in self.tables:
            self.kind = "glyf"
            loca = self._t(b"loca")
            if self.loc_long:
                self.loca = struct.unpack(">%dI" % (len(loca) // 4), loca)
            else:
                self.loca = [2 * v for v in struct.unpack(">%dH" % (len(loca) // 2), loca)]
            self.glyf = self._t(b"glyf")
        elif b"CFF " in self.tables:
            self.kind = "cff"
            self.cff = _CFF(self._t(b"CFF "))
        else:
            raise FontError("no TrueType or CFF outlines (CFF2/bitmap font?)")
        self._glyphs = {}

    def _t(self, tag, required=True):
        if tag not in self.tables:
            if required:
                raise FontError("font has no %s table" % tag.decode().strip())
            return None
        o, n = self.tables[tag]
        return self.data[o:o + n]

    def _cmap(self):
        cm = self._t(b"cmap")
        n = struct.unpack(">H", cm[2:4])[0]
        best = None
        for i in range(n):
            pid, eid, off = struct.unpack(">HHI", cm[4 + 8 * i:12 + 8 * i])
            fmt = struct.unpack(">H", cm[off:off + 2])[0]
            rank = {(3, 10): 0, (0, 4): 1, (0, 6): 1, (3, 1): 2, (0, 3): 3, (3, 0): 4}.get((pid, eid), 5)
            if fmt in (4, 12) and (best is None or rank < best[0]):
                best = (rank, off, fmt, pid == 3 and eid == 0)
        if best is None:
            raise FontError("no usable cmap")
        _, off, fmt, symbol = best
        out = {}
        if fmt == 4:
            segx2 = struct.unpack(">H", cm[off + 6:off + 8])[0]
            nseg = segx2 // 2
            ends = struct.unpack(">%dH" % nseg, cm[off + 14:off + 14 + segx2])
            starts = struct.unpack(">%dH" % nseg, cm[off + 16 + segx2:off + 16 + 2 * segx2])
            deltas = struct.unpack(">%dh" % nseg, cm[off + 16 + 2 * segx2:off + 16 + 3 * segx2])
            ro_pos = off + 16 + 3 * segx2
            ranges = struct.unpack(">%dH" % nseg, cm[ro_pos:ro_pos + segx2])
            for k in range(nseg):
                for c in range(starts[k], ends[k] + 1):
                    if c == 0xFFFF:
                        continue
                    if ranges[k] == 0:
                        g = (c + deltas[k]) & 0xFFFF
                    else:
                        p = ro_pos + 2 * k + ranges[k] + 2 * (c - starts[k])
                        g = struct.unpack(">H", cm[p:p + 2])[0]
                        if g:
                            g = (g + deltas[k]) & 0xFFFF
                    if g:
                        out[c] = g
        else:
            ng = struct.unpack(">I", cm[off + 12:off + 16])[0]
            for k in range(ng):
                s, e, g = struct.unpack(">III", cm[off + 16 + 12 * k:off + 28 + 12 * k])
                for c in range(s, e + 1):
                    out[c] = g + c - s
        if symbol:                       # symbol fonts map U+F0xx
            for c in list(out):
                if 0xF000 <= c <= 0xF0FF:
                    out.setdefault(c - 0xF000, out[c])
        return out

    def _kern(self):
        """Pair kerning from the legacy 'kern' table (what GDI, and so
        Altium, applies; GPOS kerning is ignored there too)."""
        k = self._t(b"kern", required=False)
        out = {}
        if not k or len(k) < 4:
            return out
        if struct.unpack(">H", k[0:2])[0] != 0:
            return out
        n, pos = struct.unpack(">H", k[2:4])[0], 4
        for _ in range(n):
            ln, cov = struct.unpack(">HH", k[pos + 2:pos + 6])
            if cov >> 8 == 0 and cov & 1:
                npairs = struct.unpack(">H", k[pos + 6:pos + 8])[0]
                for j in range(npairs):
                    l, r, v = struct.unpack(">HHh", k[pos + 14 + 6 * j:pos + 20 + 6 * j])
                    out[(l, r)] = v
            pos += ln
        return out

    def advance(self, gid):
        return self.advances[min(gid, len(self.advances) - 1)] if self.advances else self.upem / 2

    def glyph(self, gid, tol=2.0):
        """Closed polylines (font units) for glyph gid."""
        key = (gid, tol)
        g = self._glyphs.get(key)
        if g is None:
            g = self._cff_glyph(gid, tol) if self.kind == "cff" else self._tt_glyph(gid, tol, 0)
            self._glyphs[key] = g
        return g

    def _cff_glyph(self, gid, tol):
        return self.cff.glyph(gid, tol)

    def _tt_glyph(self, gid, tol, depth):
        if gid + 1 >= len(self.loca) or depth > 8:
            return []
        a, b = self.loca[gid], self.loca[gid + 1]
        if b <= a:
            return []
        d = self.glyf[a:b]
        nc = struct.unpack(">h", d[0:2])[0]
        if nc >= 0:
            return self._tt_simple(d, nc, tol)
        out, pos = [], 10
        while True:
            flags, sub = struct.unpack(">HH", d[pos:pos + 4])
            pos += 4
            if flags & 1:
                a1, a2 = struct.unpack(">hh", d[pos:pos + 4])
                pos += 4
            else:
                a1, a2 = struct.unpack(">bb", d[pos:pos + 2])
                pos += 2
            xx, xy, yx, yy = 1.0, 0.0, 0.0, 1.0
            if flags & 0x8:
                xx = yy = struct.unpack(">h", d[pos:pos + 2])[0] / 16384.0
                pos += 2
            elif flags & 0x40:
                xx, yy = [v / 16384.0 for v in struct.unpack(">hh", d[pos:pos + 4])]
                pos += 4
            elif flags & 0x80:
                xx, xy, yx, yy = [v / 16384.0 for v in struct.unpack(">hhhh", d[pos:pos + 8])]
                pos += 8
            dx, dy = (a1, a2) if flags & 2 else (0, 0)   # point matching: rare, ignored
            for c in self._tt_glyph(sub, tol, depth + 1):
                out.append([(xx * x + yx * y + dx, xy * x + yy * y + dy) for x, y in c])
            if not flags & 0x20:
                break
        return out

    @staticmethod
    def _tt_simple(d, nc, tol):
        if nc == 0:
            return []
        ends = struct.unpack(">%dH" % nc, d[10:10 + 2 * nc])
        npts = ends[-1] + 1
        pos = 10 + 2 * nc
        ilen = struct.unpack(">H", d[pos:pos + 2])[0]
        pos += 2 + ilen
        flags = []
        while len(flags) < npts:
            f = d[pos]
            pos += 1
            flags.append(f)
            if f & 8:
                r = d[pos]
                pos += 1
                flags.extend([f] * r)
        flags = flags[:npts]
        xs, ys = [], []
        for coords, short, same in ((xs, 2, 16), (ys, 4, 32)):
            v = 0
            for f in flags:
                if f & short:
                    dv = d[pos]
                    pos += 1
                    v += dv if f & same else -dv
                elif not f & same:
                    v += struct.unpack(">h", d[pos:pos + 2])[0]
                    pos += 2
                coords.append(v)
        out, start = [], 0
        for e in ends:
            pts = [(xs[k], ys[k], flags[k] & 1) for k in range(start, e + 1)]
            start = e + 1
            if len(pts) < 2:
                continue
            # start on an on-curve point (or the midpoint of two off-curve ones)
            k0 = next((k for k, p in enumerate(pts) if p[2]), None)
            if k0 is None:
                p, q = pts[0], pts[1]
                pts = [((p[0] + q[0]) / 2, (p[1] + q[1]) / 2, 1)] + pts[1:] + pts[:1]
            else:
                pts = pts[k0:] + pts[:k0]
            pts.append(pts[0])
            poly = [(pts[0][0], pts[0][1])]
            ctrl = None
            for x, y, on in pts[1:]:
                if on:
                    if ctrl is None:
                        poly.append((x, y))
                    else:
                        _quad(poly, poly[-1], ctrl, (x, y), tol)
                        ctrl = None
                else:
                    if ctrl is not None:
                        mid = ((ctrl[0] + x) / 2, (ctrl[1] + y) / 2)
                        _quad(poly, poly[-1], ctrl, mid, tol)
                    ctrl = (x, y)
            c = _close(poly)
            if c:
                out.append(c)
        return out

    def layout(self, text, tol=2.0):
        """[(contours, x offset)] for one line of text, plus the advance width."""
        out, x, prev = [], 0.0, None
        for ch in text:
            gid = self.cmap.get(ord(ch), 0)
            if prev is not None:
                x += self.kern.get((prev, gid), 0)
            cs = self.glyph(gid, tol)
            if cs:
                out.append((cs, x))
            x += self.advance(gid)
            prev = gid
        return out, x


def _families(data, off):
    """Family names (name IDs 1 and 16) of the font at table-directory off."""
    num = struct.unpack(">H", data[off + 4:off + 6])[0]
    for i in range(num):
        rec = off + 12 + 16 * i
        if data[rec:rec + 4] == b"name":
            o, n = struct.unpack(">II", data[rec + 8:rec + 16])
            return _name_families(data[o:o + n])
    return []


def _name_families(nm, with_style=False):
    """Family names (IDs 1 and 16) from a name table; with_style: a list of
    (family, subfamily) pairs (IDs 1+2 and 16+17) instead."""
    recs = {}
    try:
        count, soff = struct.unpack(">HH", nm[2:6])
        for i in range(count):
            pid, eid, lid, nid, ln, so = struct.unpack(">HHHHHH", nm[6 + 12 * i:18 + 12 * i])
            if nid not in (1, 2, 16, 17):
                continue
            raw = nm[soff + so:soff + so + ln]
            s = raw.decode("utf-16-be", "ignore") if pid in (0, 3) else raw.decode("latin-1")
            if s:
                recs.setdefault(nid, s)
    except struct.error:
        pass
    pairs = []
    for fam_id, sub_id in ((16, 17), (1, 2)):
        if fam_id in recs:
            pair = (recs[fam_id], recs.get(sub_id, recs.get(2, "Regular")))
            if pair not in pairs:
                pairs.append(pair)
    if with_style:
        return pairs
    out = []
    for fam, _ in pairs:
        if fam not in out:
            out.append(fam)
    return out


def file_families(path, with_style=False):
    """Family names in a font file (with_style: (family, subfamily) pairs),
    reading only its table directory and name table."""
    with open(path, "rb") as fh:
        hdr = fh.read(12)
        offs = [0]
        if hdr[:4] == b"ttcf":
            n = struct.unpack(">I", hdr[8:12])[0]
            offs = struct.unpack(">%dI" % n, fh.read(4 * n))
        names = []
        for off in offs:
            fh.seek(off)
            d = fh.read(12)
            num = struct.unpack(">H", d[4:6])[0]
            dirs = fh.read(16 * num)
            for i in range(num):
                if dirs[16 * i:16 * i + 4] == b"name":
                    o, ln = struct.unpack(">II", dirs[16 * i + 8:16 * i + 16])
                    fh.seek(o)
                    names.extend(_name_families(fh.read(ln), with_style))
        return names


# -- text to filled shapes --------------------------------------------------------
def _inside(pt, loop):
    x, y = pt
    inside = False
    n = len(loop)
    for i in range(n):
        x0, y0 = loop[i]
        x1, y1 = loop[i - 1]
        if (y0 > y) != (y1 > y) and x < (x1 - x0) * (y - y0) / (y1 - y0) + x0:
            inside = not inside
    return inside


def classify(contours):
    """[(outer, [holes])] for one glyph's contours, filled by the nonzero
    rule fonts use.

    Contours are nested by containment; the winding number inside each is
    its parent's plus its own direction. That works whichever way a font
    draws its outlines (some mix both, e.g. ROG Fonts): a counter wound
    against its outline becomes a hole, overlapping strokes wound the same
    way stay filled, and an island inside a counter is filled again.
    """
    items = [(c, area(c)) for c in contours if c and len(c) >= 3]
    items = [it for it in items if abs(it[1]) > 1e-12]
    parent = []
    for i, (c, a) in enumerate(items):
        best = None
        for j, (d, b) in enumerate(items):
            if i == j or abs(b) <= abs(a):
                continue
            if _inside(c[0], d) and (best is None or abs(b) < abs(items[best][1])):
                best = j
        parent.append(best)
    # winding number inside each contour (nonzero rule): its parent's plus
    # its own direction; parents are larger, so handle the largest first
    order = sorted(range(len(items)), key=lambda i: -abs(items[i][1]))
    wind = {}
    for i in order:
        p = parent[i]
        wind[i] = (wind[p] if p is not None else 0) + (1 if items[i][1] > 0 else -1)
    shapes, owner = [], {}
    for i in order:
        c = items[i][0]
        p = parent[i]
        inside_filled = p is not None and wind[p] != 0
        if wind[i] != 0:
            # filled: an outline, or a stroke over filled area (its own
            # solid, unioned with the rest later)
            owner[i] = len(shapes)
            shapes.append((c, []))
        elif inside_filled:
            # a counter: cut it from the nearest filled contour around it
            q = p
            while q is not None and q not in owner:
                q = parent[q]
            if q is not None:
                shapes[owner[q]][1].append(c)
    return shapes


def text_shapes(font, text, height, mirrored=False, shear=0.0, tol_mm=0.004, min_step=0.001):
    """Filled shapes for text set in font at Altium TrueType height (mm).

    Returns a list of glyphs, each a list of (outer, holes) polylines in mm:
    baseline of the last line at y=0, text starting at x=0 (mirrored: drawn
    leftwards from 0). shear slants it (synthetic italic).
    """
    s = height / font.cell
    tol = max(tol_mm / s, 1e-3)
    lines = text.split("\n")
    out = []
    for li, line in enumerate(lines):
        y0 = (len(lines) - 1 - li) * height
        for contours, dx in font.layout(line, tol)[0]:
            pts_c = []
            for c in contours:
                pts = []
                for x, y in c:
                    X = (x + dx) * s + y * s * shear
                    Y = y * s + y0
                    if mirrored:
                        X = -X
                    if not pts or abs(X - pts[-1][0]) + abs(Y - pts[-1][1]) > min_step:
                        pts.append((X, Y))
                while len(pts) > 3 and abs(pts[0][0] - pts[-1][0]) + abs(pts[0][1] - pts[-1][1]) <= min_step:
                    pts.pop()
                if len(pts) >= 3:
                    pts_c.append(pts)
            shapes = classify(pts_c)
            if shapes:
                out.append(shapes)
    return out
