"""
Builds a Fusion 360 model from a parsed Altium PcbDoc (see altium_pcbdoc.py).

Coordinate conventions
----------------------
* Fusion's internal length unit is the centimetre; the parser returns mm.
* The board's bottom face sits on Z = 0 and its top face on Z = thickness.
* XY are Altium board coordinates, shifted so the Altium user origin (or the
  board's lower-left corner) lands on the Fusion origin.
"""

import math
import os
import tempfile

import adsk.core
import adsk.fusion

CM = 0.1  # mm -> cm

BOARD_RGB = (24, 110, 60)
COPPER_RGB = (200, 140, 60)
PLACEHOLDER_RGB = (60, 60, 60)


class Options:
    def __init__(self):
        self.thickness_mm = None        # None = use stackup value
        self.pad_holes = True
        self.via_holes = False
        self.cutouts = True
        self.step_models = True
        self.extruded_bodies = True
        self.placeholders = True
        self.placeholder_height_mm = 1.0
        self.origin = "altium"          # 'altium' | 'board' | 'absolute'
        self.new_document = True


# ----------------------------------------------------------------------------
# Small math helpers (pure python, kept separate so they are unit-testable)
# ----------------------------------------------------------------------------

def rot_x(a):
    c, s = math.cos(math.radians(a)), math.sin(math.radians(a))
    return [[1, 0, 0], [0, c, -s], [0, s, c]]


def rot_y(a):
    c, s = math.cos(math.radians(a)), math.sin(math.radians(a))
    return [[c, 0, s], [0, 1, 0], [-s, 0, c]]


def rot_z(a):
    c, s = math.cos(math.radians(a)), math.sin(math.radians(a))
    return [[c, -s, 0], [s, c, 0], [0, 0, 1]]


def matmul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def body_rotation(body):
    """Rotation matrix placing an Altium STEP model in board space.

    Altium stores MODEL.3D.ROTZ as an absolute angle (it already includes the
    component rotation), and ROTX/ROTY as the model's own base orientation.
    Bottom-side bodies are flipped about the board's Y axis (Altium mirrors
    left/right when a part is sent to the bottom side).
    """
    rx, ry, rz = body.rot3d
    r = matmul(rot_y(ry), rot_x(rx))
    if body.bottom:
        r = matmul(rot_y(180.0), r)
    return matmul(rot_z(rz + body.rotation), r)


def arc_sweep(seg):
    """Signed sweep (radians) from seg.start to seg.end around seg.center."""
    cx, cy = seg.center
    a0 = math.atan2(seg.start[1] - cy, seg.start[0] - cx)
    a1 = math.atan2(seg.end[1] - cy, seg.end[0] - cx)
    sweep = (a1 - a0) % (2 * math.pi)
    if sweep < 1e-9:
        sweep = 2 * math.pi
    return sweep if seg.ccw else sweep - 2 * math.pi


def bbox(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def safe_name(s):
    bad = '<>:"/\\|?*'
    out = "".join("_" if ch in bad else ch for ch in s).strip()
    return out or "unnamed"


# ----------------------------------------------------------------------------
# Builder
# ----------------------------------------------------------------------------

class Builder:
    def __init__(self, doc, options, name, progress=None):
        self.doc = doc
        self.opt = options
        self.name = name
        self.progress = progress
        self.app = adsk.core.Application.get()
        self.log = []
        self._appearances = {}
        self._model_components = {}
        self._tmpdir = None

        if options.origin == "altium":
            self.ox, self.oy = doc.origin
        elif options.origin == "board" and doc.bounds():
            b = doc.bounds()
            self.ox, self.oy = b[0], b[1]
        else:
            self.ox, self.oy = 0.0, 0.0

        t = options.thickness_mm if options.thickness_mm else doc.thickness
        self.t = t

    # -- helpers -------------------------------------------------------------
    def P(self, x, y, z=0.0):
        """Board-space mm -> Fusion Point3D (cm)."""
        return adsk.core.Point3D.create((x - self.ox) * CM, (y - self.oy) * CM, z * CM)

    def step(self, msg, value=None):
        if self.progress:
            if value is not None:
                self.progress.progressValue = int(value)
            self.progress.message = msg
            if self.progress.wasCancelled:
                raise KeyboardInterrupt("Import cancelled")
        adsk.doEvents()

    def appearance(self, rgb, opacity=1.0):
        key = (tuple(rgb), round(opacity, 2))
        if key in self._appearances:
            return self._appearances[key]
        design = self.design
        name = "Altium RGB %d-%d-%d" % tuple(rgb)
        app = design.appearances.itemByName(name)
        if not app:
            base = None
            for lib in self.app.materialLibraries:
                try:
                    base = lib.appearances.itemByName("Plastic - Glossy (Black)") or \
                        lib.appearances.itemByName("Paint - Enamel Glossy (Black)")
                except Exception:
                    base = None
                if base:
                    break
            if not base:
                self._appearances[key] = None
                return None
            app = design.appearances.addByCopy(base, name)
            for prop in app.appearanceProperties:
                cp = adsk.core.ColorProperty.cast(prop)
                if cp and cp.id in ("opaque_albedo", "generic_diffuse", "metal_f0"):
                    try:
                        cp.value = adsk.core.Color.create(rgb[0], rgb[1], rgb[2], 255)
                    except Exception:
                        pass
        self._appearances[key] = app
        return app

    def add_loop(self, sketch, segs):
        lines = sketch.sketchCurves.sketchLines
        arcs = sketch.sketchCurves.sketchArcs
        circles = sketch.sketchCurves.sketchCircles
        if len(segs) == 1 and segs[0].center is not None:
            s = segs[0]
            r = math.dist(s.start, s.center)
            circles.addByCenterRadius(self.P(*s.center), r * CM)
            return
        first_pt = prev_pt = None
        for i, s in enumerate(segs):
            last = i == len(segs) - 1
            if s.center is None:
                start = prev_pt if prev_pt is not None else self.P(*s.start)
                end = first_pt if (last and first_pt is not None) else self.P(*s.end)
                ent = lines.addByTwoPoints(start, end)
                sp, ep = ent.startSketchPoint, ent.endSketchPoint
            else:
                sweep = arc_sweep(s)
                ent = arcs.addByCenterStartSweep(self.P(*s.center), self.P(*s.start), sweep)
                # Fusion normalises arcs to CCW, so match endpoints by distance
                a, b = ent.startSketchPoint, ent.endSketchPoint
                want = self.P(*s.start)
                if a.geometry.distanceTo(want) > b.geometry.distanceTo(want):
                    a, b = b, a
                sp, ep = a, b
                if prev_pt is not None and hasattr(prev_pt, "merge"):
                    try:
                        prev_pt.merge(sp)
                        sp = prev_pt
                    except Exception:
                        pass
                if last and first_pt is not None and hasattr(first_pt, "merge"):
                    try:
                        ep.merge(first_pt)
                    except Exception:
                        pass
            if first_pt is None:
                first_pt = sp
            prev_pt = ep

    def add_slot(self, sketch, x, y, width, length, angle_deg):
        r = width / 2.0
        half = max(length / 2.0 - r, 0.0)
        a = math.radians(angle_deg)
        ux, uy = math.cos(a), math.sin(a)
        if half < 1e-4:
            sketch.sketchCurves.sketchCircles.addByCenterRadius(self.P(x, y), r * CM)
            return
        from altium_pcbdoc import Segment  # local import keeps module importable
        c1 = (x - ux * half, y - uy * half)
        c2 = (x + ux * half, y + uy * half)
        nx, ny = -uy * r, ux * r
        p1 = (c1[0] - nx, c1[1] - ny)
        p2 = (c2[0] - nx, c2[1] - ny)
        p3 = (c2[0] + nx, c2[1] + ny)
        p4 = (c1[0] + nx, c1[1] + ny)
        self.add_loop(sketch, [Segment(p1, p2), Segment(p2, p3, c2, True),
                               Segment(p3, p4), Segment(p4, p1, c1, True)])

    # -- main ----------------------------------------------------------------
    def build(self):
        app = self.app
        if self.opt.new_document:
            d = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
            design = adsk.fusion.Design.cast(app.activeProduct)
            # Direct modelling: no timeline, much faster for hundreds of parts
            design.designType = adsk.fusion.DesignTypes.DirectDesignType
            try:
                d.name = self.name
            except Exception:
                pass
        else:
            design = adsk.fusion.Design.cast(app.activeProduct)
            if not design:
                raise RuntimeError("No active Fusion design")
        self.design = design
        root = design.rootComponent

        top_occ = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
        self.pcb = top_occ.component
        self.pcb.name = safe_name(self.name)

        self.step("Building board outline", 5)
        self.build_board()

        self.step("Building components", 30)
        self.build_components()

        if self._tmpdir:
            try:
                for f in os.listdir(self._tmpdir):
                    os.remove(os.path.join(self._tmpdir, f))
                os.rmdir(self._tmpdir)
            except OSError:
                pass

        if design.designType == adsk.fusion.DesignTypes.ParametricDesignType:
            try:
                design.snapshots.add()   # capture the occurrence positions
            except Exception:
                pass
        app.activeViewport.fit()
        return self.log

    # -- board ---------------------------------------------------------------
    def build_board(self):
        doc = self.doc
        occ = self.pcb.occurrences.addNewComponent(adsk.core.Matrix3D.create())
        comp = occ.component
        comp.name = "Board"
        if not doc.outline:
            raise RuntimeError("The PcbDoc has no board outline")

        sk = comp.sketches.add(comp.xYConstructionPlane)
        sk.name = "Board outline"
        sk.isComputeDeferred = True
        self.add_loop(sk, doc.outline)
        sk.isComputeDeferred = False
        if sk.profiles.count == 0:
            raise RuntimeError("Board outline did not form a closed profile")
        prof = max((sk.profiles.item(i) for i in range(sk.profiles.count)),
                   key=lambda p: p.areaProperties().area)
        ext = comp.features.extrudeFeatures.addSimple(
            prof, adsk.core.ValueInput.createByReal(self.t * CM),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
        board = ext.bodies.item(0)
        board.name = "PCB"
        ap = self.appearance(BOARD_RGB)
        if ap:
            board.appearance = ap

        # Holes and cutouts, all cut in one feature
        cut = comp.sketches.add(comp.xYConstructionPlane)
        cut.name = "Holes and cutouts"
        cut.isComputeDeferred = True
        n = 0
        if self.opt.cutouts:
            for loop in doc.cutouts:
                self.add_loop(cut, loop)
                n += 1
        if self.opt.pad_holes:
            for p in doc.pads:
                if p.hole <= 0:
                    continue
                if p.hole_shape == 2 and p.slot_len > p.hole:
                    self.add_slot(cut, p.x, p.y, p.hole, p.slot_len, p.rotation + p.slot_rotation)
                else:
                    cut.sketchCurves.sketchCircles.addByCenterRadius(self.P(p.x, p.y), p.hole / 2 * CM)
                n += 1
        if self.opt.via_holes:
            for v in doc.vias:
                if v.hole > 0:
                    cut.sketchCurves.sketchCircles.addByCenterRadius(self.P(v.x, v.y), v.hole / 2 * CM)
                    n += 1
        cut.isComputeDeferred = False
        if n and cut.profiles.count:
            self.step("Cutting %d holes/cutouts" % n, 20)
            profs = adsk.core.ObjectCollection.create()
            for i in range(cut.profiles.count):
                profs.add(cut.profiles.item(i))
            ei = comp.features.extrudeFeatures.createInput(
                profs, adsk.fusion.FeatureOperations.CutFeatureOperation)
            ei.setDistanceExtent(False, adsk.core.ValueInput.createByReal(self.t * CM))
            ei.participantBodies = [board]
            try:
                comp.features.extrudeFeatures.add(ei)
            except Exception as e:  # keep going; holes are nice-to-have
                self.log.append("Hole cut failed: %s" % e)
        cut.isVisible = False
        sk.isVisible = False
        self.log.append("Board: %.3f mm thick, %d holes/cutouts" % (self.t, n))

    # -- components ----------------------------------------------------------
    def build_components(self):
        doc = self.doc
        occ = self.pcb.occurrences.addNewComponent(adsk.core.Matrix3D.create())
        holder = occ.component
        holder.name = "Components"
        total = max(len(doc.components), 1)
        stats = {"step": 0, "extruded": 0, "placeholder": 0, "failed": 0}

        for i, c in enumerate(doc.components):
            self.step("Component %d/%d: %s" % (i + 1, total, c.designator),
                      30 + 70.0 * i / total)
            c_occ = holder.occurrences.addNewComponent(adsk.core.Matrix3D.create())
            cc = c_occ.component
            cc.name = safe_name(c.designator + (" (%s)" % c.pattern if c.pattern else ""))
            made = False
            for b in c.bodies:
                try:
                    if b.embedded and b.model_id in doc.models:
                        if self.opt.step_models:
                            self.place_step(cc, b)
                            stats["step"] += 1
                            made = True
                    elif self.opt.extruded_bodies and b.outline and b.overall > b.standoff:
                        self.extrude_body(cc, b.outline, b.standoff, b.overall,
                                          b.bottom, b.color or PLACEHOLDER_RGB)
                        stats["extruded"] += 1
                        made = True
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    stats["failed"] += 1
                    self.log.append("%s: body failed (%s)" % (c.designator, e))
            if not made and self.opt.placeholders:
                bb = c.pad_bounds()
                if bb and bb[2] - bb[0] > 0 and bb[3] - bb[1] > 0:
                    h = c.height if c.height > 0.01 else self.opt.placeholder_height_mm
                    pts = [(bb[0], bb[1]), (bb[2], bb[1]), (bb[2], bb[3]), (bb[0], bb[3])]
                    try:
                        self.extrude_body(cc, pts, 0.0, h, c.bottom, PLACEHOLDER_RGB)
                        stats["placeholder"] += 1
                        made = True
                    except Exception as e:
                        stats["failed"] += 1
                        self.log.append("%s: placeholder failed (%s)" % (c.designator, e))
            if not made:
                c_occ.deleteMe()

        self.log.append("Components: %d STEP models placed, %d extruded bodies, "
                        "%d placeholder boxes, %d failures" % (
                            stats["step"], stats["extruded"], stats["placeholder"], stats["failed"]))

    def extrude_body(self, comp, pts, z0, z1, bottom, rgb):
        """Extrude a closed polygon from z0..z1 above the top (or below the bottom) face."""
        sk = comp.sketches.add(comp.xYConstructionPlane)
        sk.isComputeDeferred = True
        lines = sk.sketchCurves.sketchLines

        def draw(poly):
            ps = [self.P(x, y) for x, y in poly]
            first = prev = None
            for k in range(len(ps)):
                a = prev if prev is not None else ps[k]
                b = first if (k == len(ps) - 1 and first is not None) else ps[(k + 1) % len(ps)]
                ln = lines.addByTwoPoints(a, b)
                if first is None:
                    first = ln.startSketchPoint
                prev = ln.endSketchPoint

        draw(pts)
        sk.isComputeDeferred = False
        if sk.profiles.count == 0 and len(pts) > 4:
            # messy outline (self-touching arcs etc.): fall back to its bounding box
            sk.deleteMe()
            b = bbox(pts)
            return self.extrude_body(comp, [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])],
                                     z0, z1, bottom, rgb)
        profs = adsk.core.ObjectCollection.create()
        for i in range(sk.profiles.count):
            profs.add(sk.profiles.item(i))
        if bottom:
            start, dist = -z0, -(z1 - z0)
        else:
            start, dist = self.t + z0, z1 - z0
        ei = comp.features.extrudeFeatures.createInput(
            profs, adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
        ei.startExtent = adsk.fusion.OffsetStartDefinition.create(
            adsk.core.ValueInput.createByReal(start * CM))
        ei.setOneSideExtent(
            adsk.fusion.DistanceExtentDefinition.create(adsk.core.ValueInput.createByReal(abs(dist) * CM)),
            adsk.fusion.ExtentDirections.NegativeExtentDirection if dist < 0
            else adsk.fusion.ExtentDirections.PositiveExtentDirection)
        ext = comp.features.extrudeFeatures.add(ei)
        sk.isVisible = False
        ap = self.appearance(rgb)
        if ap:
            for i in range(ext.bodies.count):
                ext.bodies.item(i).appearance = ap
        return ext

    def body_matrix(self, b):
        r = body_rotation(b)
        z = -b.dz if b.bottom else self.t + b.dz
        tx, ty, tz = (b.x - self.ox) * CM, (b.y - self.oy) * CM, z * CM
        m = adsk.core.Matrix3D.create()
        m.setWithArray([r[0][0], r[0][1], r[0][2], tx,
                        r[1][0], r[1][1], r[1][2], ty,
                        r[2][0], r[2][1], r[2][2], tz,
                        0, 0, 0, 1])
        return m

    def place_step(self, comp, b):
        mat = self.body_matrix(b)
        existing = self._model_components.get(b.model_id)
        if existing:
            self.add_instance(comp, existing, mat)
            return
        model = self.doc.models[b.model_id]
        if not self._tmpdir:
            self._tmpdir = tempfile.mkdtemp(prefix="altium_models_")
        fname = safe_name(model.name)
        if not fname.lower().endswith((".step", ".stp")):
            fname += ".step"
        path = os.path.join(self._tmpdir, "%d_%s" % (model.index, fname))
        with open(path, "wb") as f:
            f.write(model.step)
        # Import at identity first: importing into an already-transformed
        # occurrence bakes that transform into the component's contents, which
        # then gets applied twice for every re-used instance.
        occ = comp.occurrences.addNewComponent(mat)
        mc = occ.component
        mc.name = safe_name(os.path.splitext(model.name)[0])
        im = self.app.importManager
        opts = im.createSTEPImportOptions(path)
        im.importToTarget(opts, mc)
        self._model_components[b.model_id] = (mc, mat.copy())

    @staticmethod
    def _same(m1, m2, tol=1e-6):
        return all(abs(a - b) < tol for a, b in zip(m1.asArray(), m2.asArray()))

    def add_instance(self, comp, existing, mat):
        """Place another occurrence of an already-imported model component.

        In direct-modelling designs Fusion composes the transform given to
        addExistingComponent with the first occurrence's transform
        (result = mat * first). Compensate, then verify and fall back to the
        plain call in case a Fusion update fixes that behaviour.
        """
        mc, first = existing
        if self.design.designType == adsk.fusion.DesignTypes.DirectDesignType:
            x = first.copy()
            x.invert()
            x.transformBy(mat)          # x = mat * first^-1
            occ = comp.occurrences.addExistingComponent(mc, x)
            if self._same(occ.transform2, mat):
                return occ
            occ.deleteMe()
        return comp.occurrences.addExistingComponent(mc, mat)
