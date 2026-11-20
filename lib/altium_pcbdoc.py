"""
Pure-Python reader for Altium Designer binary PCB documents (.PcbDoc).

A .PcbDoc is an OLE2 compound file. Each object kind lives in its own storage
(``Board6/Data``, ``Components6/Data``, ``Pads6/Data``...). The record layouts
used here follow KiCad's Altium importer (pcbnew/pcb_io/altium, GPL-3.0); this
file is an independent re-implementation, not a copy.

Only what is needed to build a mechanical 3D model is decoded:
board outline + cutouts, stackup thickness, components, pads/vias (holes),
3D component bodies (embedded STEP models and extruded bodies).

All lengths returned are in millimetres, angles in degrees, in Altium's
board coordinate system (X right, Y up, origin = Altium absolute origin).
"""

import math
import struct
import zlib

try:  # vendored next to this file
    from . import olefile
except ImportError:  # pragma: no cover - when run as a plain script
    import os as _os
    import sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    import olefile

# Altium internal unit is 1e-7 inch (0.1 microinch = 1/10000 mil)
INTERNAL_TO_MM = 2.54e-6
MIL_TO_MM = 0.0254

LAYER_TOP = 1
LAYER_BOTTOM = 32
LAYER_MULTI = 74


class AltiumParseError(Exception):
    pass


# ----------------------------------------------------------------------------
# Low level helpers
# ----------------------------------------------------------------------------

def parse_props(raw):
    """Parse a '|KEY=VALUE|KEY=VALUE' property blob into an upper-case dict."""
    raw = raw.rstrip(b"\0")
    out = {}
    for tok in raw.split(b"|"):
        if b"=" not in tok:
            continue
        k, v = tok.split(b"=", 1)
        key = k.decode("latin-1").strip().upper()
        if key.startswith("%UTF8%"):
            val = v.decode("utf-8", "replace")
            key = key[6:]
        else:
            val = v.decode("latin-1")
            if key in out and ("%UTF8%" + key) in out:
                continue
        out[key] = val.strip()
    return out


def unit_mm(value, default=0.0):
    """Convert an Altium property length ('12.5mil', '0.3mm', '1000') to mm."""
    if value is None or value == "":
        return default
    s = value.strip().lower()
    try:
        if s.endswith("mil"):
            return float(s[:-3]) * MIL_TO_MM
        if s.endswith("mm"):
            return float(s[:-2])
        if s.endswith("in"):
            return float(s[:-2]) * 25.4
        return float(s) * INTERNAL_TO_MM
    except ValueError:
        return default


def fnum(value, default=0.0):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def fbool(value, default=False):
    if value is None:
        return default
    return str(value).strip().upper() == "TRUE"


class Reader:
    def __init__(self, data):
        self.d = data
        self.p = 0
        self.sub_end = None

    def remaining(self):
        return len(self.d) - self.p

    def u8(self):
        v = self.d[self.p]
        self.p += 1
        return v

    def u16(self):
        v = struct.unpack_from("<H", self.d, self.p)[0]
        self.p += 2
        return v

    def u32(self):
        v = struct.unpack_from("<I", self.d, self.p)[0]
        self.p += 4
        return v

    def i32(self):
        v = struct.unpack_from("<i", self.d, self.p)[0]
        self.p += 4
        return v

    def f64(self):
        v = struct.unpack_from("<d", self.d, self.p)[0]
        self.p += 8
        return v

    def mm(self):
        return self.i32() * INTERNAL_TO_MM

    def skip(self, n):
        self.p += n

    def subrecord(self):
        n = self.u32()
        self.sub_end = self.p + n
        return n

    def sub_remaining(self):
        return self.sub_end - self.p

    def end_subrecord(self):
        self.p = self.sub_end

    def props(self):
        n = self.u32()
        binary = (n & 0xFF000000) != 0
        n &= 0x00FFFFFF
        raw = self.d[self.p:self.p + n]
        self.p += n
        return {} if binary else parse_props(raw)


# ----------------------------------------------------------------------------
# Data classes (plain, so the Fusion side stays simple)
# ----------------------------------------------------------------------------

class Segment:
    """One outline piece: a line (center is None) or an arc."""
    __slots__ = ("start", "end", "center", "ccw")

    def __init__(self, start, end, center=None, ccw=True):
        self.start, self.end, self.center, self.ccw = start, end, center, ccw

    def __repr__(self):
        if self.center is None:
            return "Line(%s->%s)" % (self.start, self.end)
        return "Arc(%s->%s c=%s %s)" % (self.start, self.end, self.center,
                                         "ccw" if self.ccw else "cw")


class Component:
    def __init__(self, index):
        self.index = index
        self.designator = ""
        self.pattern = ""
        self.x = self.y = 0.0
        self.rotation = 0.0
        self.bottom = False
        self.height = 0.0
        self.comment = ""
        self.pads = []
        self.bodies = []

    def pad_bounds(self):
        """Axis-aligned bounds (board coords) of all copper pads, or None."""
        pts = []
        for p in self.pads:
            w, h = p.size
            a = math.radians(p.rotation)
            ca, sa = abs(math.cos(a)), abs(math.sin(a))
            hw = (w * ca + h * sa) / 2
            hh = (w * sa + h * ca) / 2
            pts.append((p.x - hw, p.y - hh))
            pts.append((p.x + hw, p.y + hh))
        if not pts:
            return None
        xs = [q[0] for q in pts]
        ys = [q[1] for q in pts]
        return min(xs), min(ys), max(xs), max(ys)


class Pad:
    __slots__ = ("name", "component", "x", "y", "size", "rotation", "hole",
                 "hole_shape", "slot_len", "slot_rotation", "plated", "layer")


class Via:
    __slots__ = ("x", "y", "diameter", "hole")


class Body:
    """A 3D component body: embedded STEP model or a simple extrusion."""

    def __init__(self):
        self.component = None
        self.model_id = ""
        self.model_name = ""
        self.embedded = False
        self.x = self.y = 0.0          # model origin, board coords (mm)
        self.rotation = 0.0            # 2D rotation about Z (deg)
        self.rot3d = (0.0, 0.0, 0.0)   # model rotation X, Y, Z (deg)
        self.dz = 0.0                  # model offset from board surface (mm)
        self.bottom = False            # projected onto bottom side
        self.standoff = 0.0
        self.overall = 0.0
        self.outline = []              # [(x, y)] for extruded bodies
        self.color = None              # (r, g, b) 0..255
        self.opacity = 1.0


class Model:
    def __init__(self, index, model_id, name, step_bytes):
        self.index = index
        self.id = model_id
        self.name = name
        self.step = step_bytes


class PcbDoc:
    def __init__(self):
        self.outline = []            # [Segment] closed loop
        self.cutouts = []            # [[Segment]] closed loops
        self.thickness = 1.6
        self.origin = (0.0, 0.0)     # Altium user origin (mm)
        self.stackup = []            # [(name, copper_mm, dielectric_mm)]
        self.components = []
        self.pads = []
        self.vias = []
        self.bodies = []
        self.models = {}             # id -> Model
        self.warnings = []

    # convenience -----------------------------------------------------------
    def bounds(self):
        xs, ys = [], []
        for s in self.outline:
            for pt in (s.start, s.end):
                xs.append(pt[0])
                ys.append(pt[1])
        if not xs:
            return None
        return min(xs), min(ys), max(xs), max(ys)

    def summary(self):
        b = self.bounds()
        size = "%.2f x %.2f mm" % (b[2] - b[0], b[3] - b[1]) if b else "no outline"
        emb = sum(1 for x in self.bodies if x.embedded and x.model_id in self.models)
        return ("Board %s, %.3f mm thick, %d cutouts, %d components, %d pads, "
                "%d vias, %d bodies (%d embedded STEP), %d models" % (
                    size, self.thickness, len(self.cutouts), len(self.components),
                    len(self.pads), len(self.vias), len(self.bodies), emb,
                    len(self.models)))


# ----------------------------------------------------------------------------
# Geometry from Altium vertex lists
# ----------------------------------------------------------------------------

def _vertices_to_segments(verts):
    """verts: list of (is_arc, (x,y), (cx,cy), r, sa, ea). Returns closed [Segment]."""
    pts = []  # sequence of (point, arc_info_to_reach_it)
    path = []  # list of ('pt', p) or ('arc', start, end, center, ccw)
    for is_arc, pos, center, r, sa, ea in verts:
        if is_arc and r > 0:
            a0, a1 = math.radians(sa), math.radians(ea)
            s = (center[0] + r * math.cos(a0), center[1] + r * math.sin(a0))
            e = (center[0] + r * math.cos(a1), center[1] + r * math.sin(a1))
            sweep = (ea - sa) % 360.0
            if sweep < 0.2 or math.dist(s, e) < 1e-4 and sweep < 1:
                path.append(("pt", s))
                path.append(("pt", e))
                continue
            if math.dist(s, pos) <= math.dist(e, pos):
                path.append(("arc", s, e, center, True))
            else:
                path.append(("arc", e, s, center, False))
        else:
            path.append(("pt", pos))

    # Flatten to an ordered point list remembering which consecutive pairs are arcs
    seq = []      # points
    arcs = {}     # index i -> (center, ccw) meaning seq[i] -> seq[i+1] is an arc
    for item in path:
        if item[0] == "pt":
            seq.append(item[1])
        else:
            _, s, e, c, ccw = item
            seq.append(s)
            arcs[len(seq) - 1] = (c, ccw)
            seq.append(e)

    segs = []
    n = len(seq)
    for i in range(n):
        a, b = seq[i], seq[(i + 1) % n]
        if i in arcs:
            c, ccw = arcs[i]
            segs.append(Segment(a, b, c, ccw))
        elif math.dist(a, b) > 1e-6:
            segs.append(Segment(a, b))
    return segs


def _board_outline(props):
    verts = []
    i = 0
    while "VX%d" % i in props and "VY%d" % i in props:
        verts.append((
            fnum(props.get("KIND%d" % i)) != 0,
            (unit_mm(props["VX%d" % i]), unit_mm(props["VY%d" % i])),
            (unit_mm(props.get("CX%d" % i)), unit_mm(props.get("CY%d" % i))),
            unit_mm(props.get("R%d" % i)),
            fnum(props.get("SA%d" % i)),
            fnum(props.get("EA%d" % i)),
        ))
        i += 1
    # Altium repeats the first vertex at the end; drop duplicates
    if len(verts) > 1 and not verts[-1][0] and math.dist(verts[-1][1], verts[0][1]) < 1e-6:
        verts.pop()
    return _vertices_to_segments(verts)


def _stackup(props):
    layers = {}
    i = 1
    while "LAYER%dNAME" % i in props:
        pre = "LAYER%d" % i
        layers[i] = (
            props[pre + "NAME"],
            int(fnum(props.get(pre + "NEXT"), 0)),
            unit_mm(props.get(pre + "COPTHICK"), 1.4 * MIL_TO_MM),
            unit_mm(props.get(pre + "DIELHEIGHT"), 60 * MIL_TO_MM),
        )
        i += 1
    out = []
    lid, guard = LAYER_TOP, 0
    while lid in layers and guard < 64:
        name, nxt, cu, diel = layers[lid]
        last = nxt == 0 or nxt not in layers
        out.append((name, cu, 0.0 if last else diel))
        if last:
            break
        lid, guard = nxt, guard + 1
    return out


# ----------------------------------------------------------------------------
# Main reader
# ----------------------------------------------------------------------------

class PcbDocReader:
    def __init__(self, path):
        self.path = path
        try:
            self.ole = olefile.OleFileIO(path)
        except Exception as e:  # noqa
            raise AltiumParseError(
                "Not an Altium binary PcbDoc (OLE) file: %s. ASCII PcbDoc files are "
                "not supported; re-save as binary in Altium." % e)
        self.doc = PcbDoc()

    def _stream(self, name):
        path = name + "/Data"
        if not self.ole.exists(path):
            return b""
        return self.ole.openstream(path).read()

    def read(self, load_models=True):
        doc = self.doc
        self._board()
        self._components()
        self._pads()
        self._vias()
        self._cutouts()
        self._bodies()
        if load_models:
            self._models()
        for p in doc.pads:
            if 0 <= p.component < len(doc.components):
                doc.components[p.component].pads.append(p)
        for b in doc.bodies:
            if b.component is not None and 0 <= b.component < len(doc.components):
                doc.components[b.component].bodies.append(b)
        self.ole.close()
        return doc

    # -- Board6 --------------------------------------------------------------
    def _board(self):
        data = self._stream("Board6")
        if not data:
            raise AltiumParseError("Board6 stream missing")
        props = Reader(data).props()
        self.doc.outline = _board_outline(props)
        self.doc.origin = (unit_mm(props.get("ORIGINX")), unit_mm(props.get("ORIGINY")))
        self.doc.stackup = _stackup(props)
        if self.doc.stackup:
            t = sum(cu + d for _, cu, d in self.doc.stackup)
            if 0.05 < t < 20:
                self.doc.thickness = t
        if not self.doc.outline:
            self.doc.warnings.append("Board outline is empty")

    # -- Components6 ---------------------------------------------------------
    def _components(self):
        r = Reader(self._stream("Components6"))
        idx = 0
        while r.remaining() >= 4:
            pr = r.props()
            c = Component(idx)
            c.designator = pr.get("SOURCEDESIGNATOR", "") or "C%d" % idx
            c.pattern = pr.get("PATTERN", "")
            c.x = unit_mm(pr.get("X"))
            c.y = unit_mm(pr.get("Y"))
            c.rotation = fnum(pr.get("ROTATION"))
            c.bottom = pr.get("LAYER", "TOP").upper() == "BOTTOM"
            c.height = unit_mm(pr.get("HEIGHT"))
            c.comment = pr.get("SOURCELIBREFERENCE", "")
            self.doc.components.append(c)
            idx += 1

    # -- Pads6 ---------------------------------------------------------------
    def _pads(self):
        r = Reader(self._stream("Pads6"))
        while r.remaining() >= 4:
            start = r.p
            try:
                self.doc.pads.append(self._pad(r))
            except (struct.error, IndexError) as e:
                self.doc.warnings.append("Pads6 parse stopped at byte %d: %s" % (start, e))
                break

    def _pad(self, r):
        if r.u8() != 2:
            raise struct.error("bad pad record type")
        p = Pad()
        r.subrecord()
        n = r.u8()
        p.name = r.d[r.p:r.p + n].decode("latin-1")
        r.end_subrecord()
        for _ in range(3):
            r.subrecord()
            r.end_subrecord()
        n5 = r.subrecord()
        p.layer = r.u8()
        r.skip(2)
        r.u16()                      # net
        r.skip(2)
        p.component = r.u16()
        if p.component == 0xFFFF:
            p.component = -1
        r.skip(4)
        p.x, p.y = r.mm(), r.mm()
        top = (r.mm(), r.mm())
        r.mm(); r.mm()               # mid size
        bot = (r.mm(), r.mm())
        p.size = top if top[0] > 0 else bot
        p.hole = r.mm()
        r.skip(3)                    # shapes
        p.rotation = r.f64()
        p.plated = r.u8() != 0
        r.end_subrecord()
        p.hole_shape, p.slot_len, p.slot_rotation = 0, 0.0, 0.0
        n6 = r.subrecord()
        if n6 >= 596:
            r.skip(29 * 4 * 2 + 29 + 1)
            p.hole_shape = r.u8()
            p.slot_len = r.mm()
            p.slot_rotation = r.f64()
        r.end_subrecord()
        del n5
        return p

    # -- Vias6 ---------------------------------------------------------------
    def _vias(self):
        r = Reader(self._stream("Vias6"))
        while r.remaining() >= 4:
            if r.u8() != 3:
                self.doc.warnings.append("Vias6: unexpected record type")
                break
            r.subrecord()
            r.skip(13)
            v = Via()
            v.x, v.y = r.mm(), r.mm()
            v.diameter = r.mm()
            v.hole = r.mm()
            r.end_subrecord()
            self.doc.vias.append(v)

    # -- Regions (board cutouts) --------------------------------------------
    def _region(self, r, extended):
        r.u8()
        r.subrecord()
        r.skip(5)
        r.u16()                      # polygon
        comp = r.u16()
        r.skip(5)
        holecount = r.u16()
        r.skip(2)
        pr = r.props()
        n = r.u32()
        verts = []
        if extended:
            for _ in range(n + 1):
                is_arc = r.u8() != 0
                pos = (r.mm(), r.mm())
                ctr = (r.mm(), r.mm())
                rad = r.mm()
                a1, a2 = r.f64(), r.f64()
                verts.append((is_arc, pos, ctr, rad, a1, a2))
        else:
            for _ in range(n):
                x, y = r.f64(), r.f64()
                verts.append((False, (x * INTERNAL_TO_MM, y * INTERNAL_TO_MM), (0, 0), 0, 0, 0))
        r.end_subrecord()
        return pr, comp, holecount, verts

    def _cutouts(self):
        seen = set()
        for name, ext in (("ShapeBasedRegions6", True), ("Regions6", False)):
            r = Reader(self._stream(name))
            while r.remaining() >= 4:
                try:
                    pr, comp, _, verts = self._region(r, ext)
                except (struct.error, IndexError) as e:
                    self.doc.warnings.append("%s parse stopped: %s" % (name, e))
                    break
                if fnum(pr.get("KIND")) == 0 and fbool(pr.get("ISBOARDCUTOUT")):
                    if ext and verts and math.dist(verts[0][1], verts[-1][1]) < 1e-6 and not verts[-1][0]:
                        verts = verts[:-1]
                    key = tuple(round(v[1][0], 3) for v in verts[:4])
                    if key in seen:
                        continue          # same cutout appears in both streams
                    seen.add(key)
                    segs = _vertices_to_segments(verts)
                    if len(segs) >= 2:
                        self.doc.cutouts.append(segs)

    # -- ComponentBodies6 ----------------------------------------------------
    def _bodies(self):
        r = Reader(self._stream("ComponentBodies6"))
        while r.remaining() >= 4:
            try:
                if r.u8() != 12:
                    raise struct.error("bad body record type")
                r.subrecord()
                r.skip(7)
                comp = r.u16()
                r.skip(9)
                pr = r.props()
                outline = []
                if r.sub_remaining() >= 4:
                    n = r.u32()
                    if r.sub_remaining() >= 16 * n:
                        for _ in range(n):
                            outline.append((r.f64() * INTERNAL_TO_MM, r.f64() * INTERNAL_TO_MM))
                r.end_subrecord()
            except (struct.error, IndexError) as e:
                self.doc.warnings.append("ComponentBodies6 parse stopped: %s" % e)
                break
            b = Body()
            b.component = None if comp == 0xFFFF else comp
            b.model_id = pr.get("MODELID", "")
            b.model_name = pr.get("MODEL.NAME", "")
            b.embedded = fbool(pr.get("MODEL.EMBED"))
            b.x = unit_mm(pr.get("MODEL.2D.X"))
            b.y = unit_mm(pr.get("MODEL.2D.Y"))
            b.rotation = fnum(pr.get("MODEL.2D.ROTATION"))
            b.rot3d = (fnum(pr.get("MODEL.3D.ROTX")), fnum(pr.get("MODEL.3D.ROTY")),
                       fnum(pr.get("MODEL.3D.ROTZ")))
            b.dz = unit_mm(pr.get("MODEL.3D.DZ"))
            b.bottom = int(fnum(pr.get("BODYPROJECTION"))) == 1
            b.standoff = unit_mm(pr.get("STANDOFFHEIGHT"))
            b.overall = unit_mm(pr.get("OVERALLHEIGHT"))
            b.opacity = fnum(pr.get("BODYOPACITY3D"), 1.0)
            col = pr.get("BODYCOLOR3D")
            if col:
                v = int(fnum(col))          # Delphi TColor: 0x00BBGGRR
                b.color = (v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF)
            # Altium closes the polygon and sometimes repeats points
            pts = []
            for q in outline:
                if not pts or math.dist(pts[-1], q) > 1e-6:
                    pts.append(q)
            if len(pts) > 2 and math.dist(pts[0], pts[-1]) < 1e-6:
                pts.pop()
            b.outline = pts
            self.doc.bodies.append(b)

    # -- Models --------------------------------------------------------------
    def _models(self):
        r = Reader(self._stream("Models"))
        idx = 0
        while r.remaining() >= 4:
            pr = r.props()
            sp = "Models/%d" % idx
            if self.ole.exists(sp):
                raw = self.ole.openstream(sp).read()
                try:
                    step = zlib.decompress(raw)
                except zlib.error:
                    step = raw
                mid = pr.get("ID", "")
                self.doc.models[mid] = Model(idx, mid, pr.get("NAME", "model_%d" % idx), step)
            else:
                self.doc.warnings.append("Embedded model stream %s missing" % sp)
            idx += 1


def read_pcbdoc(path, load_models=True):
    return PcbDocReader(path).read(load_models=load_models)


if __name__ == "__main__":  # quick CLI check: python altium_pcbdoc.py board.PcbDoc
    import sys
    for fn in sys.argv[1:]:
        d = read_pcbdoc(fn)
        print(fn)
        print("  " + d.summary())
        for w in d.warnings:
            print("  warning:", w)
