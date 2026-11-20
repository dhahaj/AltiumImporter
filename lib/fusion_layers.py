"""
Builds thin copper, solder-mask and silkscreen bodies in Fusion.

Sketch profiles get very slow once thousands of traces overlap, so every
primitive is made directly as a temporary B-Rep solid (box / cylinder) with
TemporaryBRepManager and the solids are merged with a spatially ordered,
balanced tree of Boolean unions. Shapes that are not boxes or cylinders
(arc traces, copper regions and pours, text) are extruded once in a hidden
scratch design and copied in.
"""

import math

import adsk.core
import adsk.fusion

import layer_geometry as lg
import font_metrics

CM = 0.1
P3 = adsk.core.Point3D.create
V3 = adsk.core.Vector3D.create
UNION = adsk.fusion.BooleanTypes.UnionBooleanType
DIFF = adsk.fusion.BooleanTypes.DifferenceBooleanType
INTERSECT = adsk.fusion.BooleanTypes.IntersectionBooleanType


def _degenerate(q):
    """Zero-size primitives legitimately produce nothing."""
    k = q[0]
    if k == "circle":
        return q[3] <= 1e-5
    if k == "stadium":
        return q[5] <= 1e-5
    if k in ("rect", "roundrect", "octagon"):
        return q[3] <= 1e-5 or q[4] <= 1e-5
    return False


def _morton(x, y):
    """Interleave bits so that nearby primitives get merged first."""
    def spread(v):
        v &= 0xFFFF
        v = (v | (v << 8)) & 0x00FF00FF
        v = (v | (v << 4)) & 0x0F0F0F0F
        v = (v | (v << 2)) & 0x33333333
        v = (v | (v << 1)) & 0x55555555
        return v
    return spread(int(x * 4)) | (spread(int(y * 4)) << 1)


class LayerBuilder:
    def __init__(self, builder):
        self.b = builder                    # fusion_builder.Builder
        self.tb = adsk.fusion.TemporaryBRepManager.get()
        self._scratch_doc = None
        self._scratch = None
        self._text_cache = {}
        self._bad_fonts = set()
        self.fallback_fonts = {}        # font Fusion could not use -> substitute used
        self.failed = 0
        self.fail_info = {}     # kind -> [count, first error]

    # -- coordinates ---------------------------------------------------------
    def xy(self, x, y):
        return (x - self.b.ox) * CM, (y - self.b.oy) * CM

    # -- scratch design for feature-based extrusions -------------------------
    def scratch(self):
        """Component for throw-away extrusions.

        Direct-modelling target: a temporary component inside it, deleted on
        close(). Parametric target: a separate direct-modelling document
        (Fusion cannot create hidden documents), closed on close().
        """
        if self._scratch is None:
            design = self.b.design
            if design.designType == adsk.fusion.DesignTypes.DirectDesignType:
                occ = design.rootComponent.occurrences.addNewComponent(adsk.core.Matrix3D.create())
                occ.component.name = "_altium_scratch"
                self._scratch_occ = occ
                self._scratch = occ.component
            else:
                app = adsk.core.Application.get()
                target = app.activeDocument
                doc = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
                d2 = adsk.fusion.Design.cast(doc.products.itemByProductType("DesignProductType"))
                d2.designType = adsk.fusion.DesignTypes.DirectDesignType
                self._scratch_doc = doc
                self._scratch = d2.rootComponent
                try:
                    target.activate()
                except Exception:
                    pass
        return self._scratch

    def close(self):
        if getattr(self, "_scratch_occ", None):
            try:
                self._scratch_occ.deleteMe()
            except Exception:
                pass
            self._scratch_occ = None
        if self._scratch_doc:
            try:
                self._scratch_doc.close(False)
            except Exception:
                pass
        self._scratch_doc = self._scratch = None

    # -- primitive solids ----------------------------------------------------
    def _cyl(self, x, y, r, z0, z1):
        cx, cy = self.xy(x, y)
        return self.tb.createCylinderOrCone(P3(cx, cy, z0 * CM), r * CM, P3(cx, cy, z1 * CM), r * CM)

    def _box(self, cx, cy, w, h, deg, z0, z1):
        x, y = self.xy(cx, cy)
        a = math.radians(deg)
        obb = adsk.core.OrientedBoundingBox3D.create(
            P3(x, y, (z0 + z1) / 2 * CM), V3(math.cos(a), math.sin(a), 0),
            V3(-math.sin(a), math.cos(a), 0), w * CM, h * CM, (z1 - z0) * CM)
        return self.tb.createBox(obb)

    def solid(self, q, z0, z1):
        """Temporary solid for one primitive between heights z0 < z1 (mm)."""
        kind = q[0]
        tb = self.tb
        if kind == "circle":
            return self._cyl(q[1], q[2], q[3], z0, z1) if q[3] > 1e-5 else None
        if kind == "stadium":
            _, x1, y1, x2, y2, w = q
            if w <= 1e-5:
                return None
            L = math.dist((x1, y1), (x2, y2))
            c1 = self._cyl(x1, y1, w / 2, z0, z1)
            if L < 1e-5:
                return c1
            deg = math.degrees(math.atan2(y2 - y1, x2 - x1))
            body = self._box((x1 + x2) / 2, (y1 + y2) / 2, L, w, deg, z0, z1)
            tb.booleanOperation(body, c1, UNION)
            tb.booleanOperation(body, self._cyl(x2, y2, w / 2, z0, z1), UNION)
            return body
        if kind == "rect":
            _, cx, cy, w, h, deg = q
            return self._box(cx, cy, w, h, deg, z0, z1)
        if kind == "roundrect":
            _, cx, cy, w, h, deg, r = q
            r = min(r, w / 2, h / 2)
            if r < 1e-4:
                return self._box(cx, cy, w, h, deg, z0, z1)
            if abs(min(w, h) / 2 - r) < 1e-4:
                return self.solid(lg.oval_prim(cx, cy, w, h, deg), z0, z1)
            body = self._box(cx, cy, w, h - 2 * r, deg, z0, z1)
            tb.booleanOperation(body, self._box(cx, cy, w - 2 * r, h, deg, z0, z1), UNION)
            a = math.radians(deg)
            ca, sa = math.cos(a), math.sin(a)
            for dx, dy in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
                ox, oy = dx * (w / 2 - r), dy * (h / 2 - r)
                tb.booleanOperation(body, self._cyl(cx + ox * ca - oy * sa,
                                                    cy + ox * sa + oy * ca, r, z0, z1), UNION)
            return body
        if kind == "octagon":
            _, cx, cy, w, h, deg = q
            c = min(w, h) / 4.0
            k = w / 2 + h / 2 - c
            body = self._box(cx, cy, w, h, deg, z0, z1)
            diamond = self._box(cx, cy, k * math.sqrt(2), k * math.sqrt(2), deg + 45, z0, z1)
            tb.booleanOperation(body, diamond, INTERSECT)
            return body
        if kind == "poly":
            return self.poly_solid(q[1], q[2], z0, z1)
        return None

    def _box_bounds(self, z0, z1):
        """A slab covering the whole board between z0 and z1."""
        b = self.b.doc.bounds()
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        return self._box(cx, cy, (b[2] - b[0]) + 10, (b[3] - b[1]) + 10, 0, z0, z1)

    # -- general outlines via a face extrusion -------------------------------
    def _curves(self, loop):
        curves = []
        if len(loop) == 1 and loop[0].center is not None:
            s = loop[0]
            cx, cy = self.xy(*s.center)
            curves.append(adsk.core.Circle3D.createByCenter(
                P3(cx, cy, 0), V3(0, 0, 1), math.dist(s.start, s.center) * CM))
            return curves
        for s in loop:
            sx, sy = self.xy(*s.start)
            ex, ey = self.xy(*s.end)
            if s.center is None:
                if math.dist((sx, sy), (ex, ey)) > 1e-7:
                    curves.append(adsk.core.Line3D.create(P3(sx, sy, 0), P3(ex, ey, 0)))
                continue
            cx, cy = self.xy(*s.center)
            r = math.dist((sx, sy), (cx, cy))
            a0 = math.atan2(sy - cy, sx - cx)
            a1 = math.atan2(ey - cy, ex - cx)
            sweep = (a1 - a0) % (2 * math.pi) if s.ccw else -((a0 - a1) % (2 * math.pi))
            if abs(sweep) < 1e-9:
                sweep = 2 * math.pi if s.ccw else -2 * math.pi
            if math.dist((sx, sy), (ex, ey)) < 1e-9:
                # closed arc inside a multi-segment loop: split in two halves
                am = a0 + sweep / 2
                m = P3(cx + r * math.cos(am), cy + r * math.sin(am), 0)
                q1 = P3(cx + r * math.cos(a0 + sweep / 4), cy + r * math.sin(a0 + sweep / 4), 0)
                q3 = P3(cx + r * math.cos(a0 + 3 * sweep / 4), cy + r * math.sin(a0 + 3 * sweep / 4), 0)
                curves.append(adsk.core.Arc3D.createByThreePoints(P3(sx, sy, 0), q1, m))
                curves.append(adsk.core.Arc3D.createByThreePoints(m, q3, P3(ex, ey, 0)))
                continue
            # three-point arcs keep the exact shared endpoints, so neighbours
            # always meet even when Altium's centre/radius are slightly off
            am = a0 + sweep / 2
            curves.append(adsk.core.Arc3D.createByThreePoints(
                P3(sx, sy, 0), P3(cx + r * math.cos(am), cy + r * math.sin(am), 0), P3(ex, ey, 0)))
        return curves

    def poly_solid(self, outer, holes, z0, z1):
        tb = self.tb
        wires = []
        outer = lg.clean_loop(outer) if outer else None
        if not outer:
            return None
        for loop in [outer] + [lg.clean_loop(h) for h in (holes or []) if h]:
            if not loop:
                continue
            wire_body, _ = tb.createWireFromCurves(self._curves(loop), True)
            if wire_body is None or wire_body.wires.count == 0:
                if loop is outer:
                    return None
                continue
            wires.append(wire_body)
        face_body = tb.createFaceFromPlanarWires(wires)
        if face_body is None or face_body.faces.count == 0:
            return None
        comp = self.scratch()
        sheet = comp.bRepBodies.add(face_body)
        face = sheet.faces.item(0)
        ext_in = comp.features.extrudeFeatures.createInput(
            face, adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
        ext_in.setDistanceExtent(False, adsk.core.ValueInput.createByReal((z1 - z0) * CM))
        ext = comp.features.extrudeFeatures.add(ext_in)
        solid = None
        for i in range(ext.bodies.count):
            c = tb.copy(ext.bodies.item(i))
            if solid is None:
                solid = c
            else:
                tb.booleanOperation(solid, c, UNION)
        for i in range(ext.bodies.count - 1, -1, -1):
            ext.bodies.item(i).deleteMe()
        sheet.deleteMe()
        if solid is not None:
            # the face normal (hence extrude direction) follows the wire
            # orientation, so place by bounding box rather than assuming +Z
            dz = z0 * CM - solid.boundingBox.minPoint.z
            if abs(dz) > 1e-9:
                m = adsk.core.Matrix3D.create()
                m.translation = V3(0, 0, dz)
                tb.transform(solid, m)
        return solid

    # -- text ----------------------------------------------------------------
    def _text_metrics(self, t):
        """(cap height, baseline offset) in mm for Altium text height t.height.

        Stroke fonts: Altium's height is (nearly) the cap height and the
        position is on the baseline. TrueType: Altium's height is the whole
        font cell (winAscent + winDescent) and the position is the bottom of
        that cell, so the baseline sits one descent above it. Metrics come from
        the font Altium asked for, even when Fusion falls back to Arial, so the
        text keeps the size it has in Altium.
        """
        h = t.height
        if t.font_type == 1:
            cap, desc = font_metrics.metrics(t.font_name or "Arial", t.bold, t.italic)
            return h * cap, h * desc
        return h * 0.98, 0.0

    @staticmethod
    def _substitute(font):
        """(Windows font, horizontal scale) standing in for a font that isn't
        installed, chosen to keep roughly the same width so labels don't run
        into each other: condensed faces become narrowed Arial, monospaced
        ones Consolas, black/heavy weights Arial Black."""
        f = font.lower()
        if "mono" in f or "code" in f or "console" in f or "courier" in f:
            return "Consolas", 1.0
        if "black" in f or "heavy" in f:
            return "Arial Black", 1.0
        if "compressed" in f or "extra cond" in f or "extracond" in f:
            return "Arial", 0.70
        if "cond" in f or "narrow" in f:
            return "Arial", 0.82
        return "Arial", 1.0

    def _font_for(self, t):
        """(font Fusion should use, horizontal scale) for text t."""
        if t.font_type != 1 or not t.font_name:
            return "Arial", 1.0
        font = t.font_name
        if font not in self._bad_fonts:
            return font, 1.0
        sub, sx = self._substitute(font)
        if sub in self._bad_fonts:
            sub = "Arial"
        return sub, sx

    def _mark_bad(self, t, font):
        self._bad_fonts.add(font)
        if t.font_name and t.font_name in self._bad_fonts:
            sub, sx = self._substitute(t.font_name)
            if sub in self._bad_fonts:
                sub = "Arial"
            self.fallback_fonts[t.font_name] = sub + (" (narrowed)" if sx < 1 else "")

    def text_solid(self, t, z0, z1):
        """Extruded text at its Altium position. Returns a temporary solid."""
        font, sx = self._font_for(t)
        cap, base_off = self._text_metrics(t)
        key = (t.text, round(cap, 4), font, sx, t.bold, t.italic, t.mirrored,
               round(z1 - z0, 5))
        hit = self._text_cache.get(key)
        if hit is None:
            comp = self.scratch()
            sk = comp.sketches.add(comp.xYConstructionPlane)
            hc = cap * CM
            lines = t.text.split("\n")
            width = max(len(s) for s in lines) * hc * 2.0 + hc
            ti = sk.sketchTexts.createInput2(t.text, hc)
            ti.setAsMultiLine(P3(0, 0, 0), P3(width, hc * 2.4 * len(lines), 0),
                              adsk.core.HorizontalAlignments.LeftHorizontalAlignment,
                              adsk.core.VerticalAlignments.BottomVerticalAlignment, 0)
            try:
                ti.fontName = font
            except Exception:
                if font == "Arial":
                    raise
                sk.deleteMe()
                self._mark_bad(t, font)
                return self.text_solid(t, z0, z1)
            style = 0
            if t.bold:
                style |= adsk.fusion.TextStyles.TextStyleBold
            if t.italic:
                style |= adsk.fusion.TextStyles.TextStyleItalic
            if style:
                ti.textStyle = style
            if t.mirrored:
                ti.isHorizontalFlip = True
            try:
                st = sk.sketchTexts.add(ti)
            except Exception:
                if font == "Arial":
                    raise
                # font not installed for Fusion: use a stand-in
                sk.deleteMe()
                self._mark_bad(t, font)
                return self.text_solid(t, z0, z1)
            ext = comp.features.extrudeFeatures.addSimple(
                st, adsk.core.ValueInput.createByReal((z1 - z0) * CM),
                adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
            base = None
            for i in range(ext.bodies.count):
                c = self.tb.copy(ext.bodies.item(i))
                if base is None:
                    base = c
                else:
                    self.tb.booleanOperation(base, c, UNION)
            for i in range(ext.bodies.count - 1, -1, -1):
                ext.bodies.item(i).deleteMe()
            sk.deleteMe()
            if base is None:
                return None
            if abs(sx - 1.0) > 1e-6:
                # narrowed stand-in for a condensed font (scale about x=0)
                m = adsk.core.Matrix3D.create()
                m.setWithArray([sx, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1])
                self.tb.transform(base, m)
            # normalise: left ink edge at x=0 (right edge when mirrored),
            # last line's baseline stays at y=0 (Fusion's bottom alignment)
            bb = base.boundingBox
            ink_w = (bb.maxPoint.x - bb.minPoint.x) / CM
            dx = -bb.maxPoint.x if t.mirrored else -bb.minPoint.x
            m = adsk.core.Matrix3D.create()
            m.translation = V3(dx, 0, 0)
            self.tb.transform(base, m)
            hit = (base, ink_w)
            self._text_cache[key] = hit
        base, ink_w = hit
        lx, ly = self._text_offset(t, ink_w, base_off)
        if t.mirrored:
            lx = -lx
        body = self.tb.copy(base)
        m = adsk.core.Matrix3D.create()
        a = math.radians(t.rotation)
        ca, sa = math.cos(a), math.sin(a)
        x, y = self.xy(t.x, t.y)
        x += (ca * lx - sa * ly) * CM
        y += (sa * lx + ca * ly) * CM
        m.setWithArray([ca, -sa, 0, x,
                        sa, ca, 0, y,
                        0, 0, 1, z0 * CM,
                        0, 0, 0, 1])
        if not self.tb.transform(body, m):
            return None
        return body

    @staticmethod
    def _text_offset(t, ink_w, base_off):
        """Where the text block's left edge / last baseline goes, in mm, in
        the text's own (unrotated, unmirrored) frame relative to its position."""
        if not getattr(t, "frame", False) or t.box_w <= 0 or t.box_h <= 0:
            return 0.0, base_off
        n = t.text.count("\n") + 1
        block_h = n * t.height
        mg = min(t.margin, t.box_w / 2, t.box_h / 2)
        col, row = divmod((t.justify or 3) - 1, 3)   # col 0 L/1 C/2 R, row 0 T/1 C/2 B
        if col == 0:
            lx = mg
        elif col == 1:
            lx = (t.box_w - ink_w) / 2
        else:
            lx = t.box_w - mg - ink_w
        if row == 0:
            bottom = t.box_h - mg - block_h
        elif row == 1:
            bottom = (t.box_h - block_h) / 2
        else:
            bottom = mg
        return lx, bottom + base_off

    # -- assembly of a layer ---------------------------------------------------
    def union_all(self, prims, z0, z1, progress=None, extra=None):
        """Solids for all prims, merged. Returns a list of temp bodies
        (normally one; see merge)."""
        items = []
        for q in prims:
            try:
                c = lg.prim_center(q)
                items.append((_morton(c[0] - self.b.ox + 1000, c[1] - self.b.oy + 1000), q))
            except Exception as e:
                self._fail(q[0] if q else "?", "no center: %s" % e)
        items.sort(key=lambda it: it[0])
        solids = []
        for i, (_, q) in enumerate(items):
            err = "returned nothing"
            try:
                s = self.solid(q, z0, z1)
            except Exception as e:
                s, err = None, str(e).splitlines()[0][:120] if str(e) else type(e).__name__
            if s is None:
                if not _degenerate(q):
                    self._fail(q[0], err)
            else:
                solids.append(s)
            if progress and i % 200 == 0:
                progress(i, len(items))
        if extra:
            solids.extend(extra)
        return self.merge(solids)

    def union_layer(self, prims, z0, z1, progress=None, big_edges=150):
        """Like union_all, but polygons with more than big_edges edges (pours)
        stay as separate bodies. Booleans against a pour with thousands of
        edges are what makes dense boards slow, and overlapping copper looks
        the same. Returns a list of temp bodies."""
        small, big = [], []
        for q in prims:
            if q[0] == "poly" and len(q[1]) + sum(len(h) for h in q[2]) > big_edges:
                big.append(q)
            else:
                small.append(q)
        out = list(self.union_all(small, z0, z1, progress))
        for q in big:
            try:
                s = self.solid(q, z0, z1)
            except Exception as e:
                s = None
                self._fail("pour", str(e).splitlines()[0][:120] if str(e) else type(e).__name__)
            if s is not None:
                out.append(s)
            adsk.doEvents()
        return out

    def merge(self, solids):
        """Tree union. Returns a list: the merged body, plus any piece that
        could not be joined (kept as its own body rather than lost)."""
        tb = self.tb
        solids = [s for s in solids if s is not None]
        if not solids:
            return []
        loose = []
        while len(solids) > 1:
            nxt = []
            for i in range(0, len(solids) - 1, 2):
                a, b = solids[i], solids[i + 1]
                try:
                    ok = tb.booleanOperation(a, b, UNION)   # the tool body b is not modified
                except Exception:
                    ok = False
                if not ok:
                    loose.append(b)
                nxt.append(a)
            if len(solids) % 2:
                nxt.append(solids[-1])
            solids = nxt
            adsk.doEvents()
        return solids + loose

    def subtract(self, target, tools):
        """target minus tools (a body or a list of bodies)."""
        if target is None or not tools:
            return target
        if not isinstance(tools, (list, tuple)):
            tools = [tools]
        for tool in tools:
            try:
                self.tb.booleanOperation(target, self.tb.copy(tool), DIFF)
            except Exception as e:
                self._fail("subtract", str(e)[:120])
        return target

    def _fail(self, kind, err):
        self.failed += 1
        info = self.fail_info.setdefault(kind, [0, err])
        info[0] += 1

    def fail_summary(self):
        return ", ".join("%d %s (%s)" % (n, k, e) for k, (n, e) in sorted(self.fail_info.items()))
