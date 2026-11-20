"""
Turns parsed Altium primitives into closed 2D outlines per physical layer.

Pure Python (no Fusion dependency) so it can be unit-tested anywhere.
Every outline is a list of altium_pcbdoc.Segment forming a closed loop; a
"region" is (outer_loop, [hole_loops]). A full circle is a single arc
Segment whose start == end.
"""

import math

from altium_pcbdoc import Segment

LAYER_TOP = 1
LAYER_BOTTOM = 32
TOP_OVERLAY = 33
BOTTOM_OVERLAY = 34
TOP_SOLDER = 37
BOTTOM_SOLDER = 38
MULTI = 74

SHAPE_CIRCLE = 1
SHAPE_RECT = 2
SHAPE_OCTAGON = 3
ALT_ROUNDRECT = 9

EPS = 1e-6
GEOM_TOL = 1e-3   # mm; anything thinner is below what Fusion can model reliably


def _rot(pts, cx, cy, deg):
    a = math.radians(deg)
    ca, sa = math.cos(a), math.sin(a)
    return [(cx + x * ca - y * sa, cy + x * sa + y * ca) for x, y in pts]


def polygon(pts):
    n = len(pts)
    return [Segment(pts[i], pts[(i + 1) % n]) for i in range(n)
            if math.dist(pts[i], pts[(i + 1) % n]) > EPS]


def circle(cx, cy, r):
    p = (cx + r, cy)
    return [Segment(p, p, (cx, cy), True)]


def stadium(x1, y1, x2, y2, w):
    """Track with round ends."""
    r = w / 2.0
    L = math.dist((x1, y1), (x2, y2))
    if L < EPS:
        return circle(x1, y1, r)
    ux, uy = (x2 - x1) / L, (y2 - y1) / L
    nx, ny = -uy * r, ux * r
    p1, p2 = (x1 - nx, y1 - ny), (x2 - nx, y2 - ny)
    p3, p4 = (x2 + nx, y2 + ny), (x1 + nx, y1 + ny)
    return [Segment(p1, p2), Segment(p2, p3, (x2, y2), True),
            Segment(p3, p4), Segment(p4, p1, (x1, y1), True)]


def arc_track(cx, cy, radius, sa, ea, w):
    """Arc track with round ends. Returns a region (outer, holes)."""
    r = w / 2.0
    sweep = (ea - sa) % 360.0
    if sweep < 1e-6 or sweep > 360 - 1e-6:
        if radius - r > GEOM_TOL:
            return circle(cx, cy, radius + r), [circle(cx, cy, radius - r)]
        return circle(cx, cy, radius + r), []
    if radius - r <= GEOM_TOL:
        # degenerate (width >= diameter): approximate with a bounding stadium
        a0, a1 = math.radians(sa), math.radians(sa + sweep)
        s = (cx + radius * math.cos(a0), cy + radius * math.sin(a0))
        e = (cx + radius * math.cos(a1), cy + radius * math.sin(a1))
        return stadium(s[0], s[1], e[0], e[1], w), []
    a0, a1 = math.radians(sa), math.radians(sa + sweep)
    ro, ri = radius + r, radius - r

    def pt(rr, a):
        return (cx + rr * math.cos(a), cy + rr * math.sin(a))

    s_end = pt(radius, a0)
    e_end = pt(radius, a1)
    return [
        Segment(pt(ro, a0), pt(ro, a1), (cx, cy), True),
        Segment(pt(ro, a1), pt(ri, a1), e_end, True),
        Segment(pt(ri, a1), pt(ri, a0), (cx, cy), False),
        Segment(pt(ri, a0), pt(ro, a0), s_end, True),
    ], []


def rect(cx, cy, w, h, deg=0.0):
    hw, hh = w / 2.0, h / 2.0
    return polygon(_rot([(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)], cx, cy, deg))


def roundrect(cx, cy, w, h, deg, r):
    hw, hh = w / 2.0, h / 2.0
    r = max(0.0, min(r, hw, hh))
    if r < EPS:
        return rect(cx, cy, w, h, deg)
    if abs(hw - r) < EPS and abs(hh - r) < EPS:
        return circle(cx, cy, r)
    if abs(min(hw, hh) - r) < EPS:
        return oval(cx, cy, w, h, deg)
    pts = [(-hw + r, -hh), (hw - r, -hh), (hw, -hh + r), (hw, hh - r),
           (hw - r, hh), (-hw + r, hh), (-hw, hh - r), (-hw, -hh + r)]
    ctr = [(hw - r, -hh + r), (hw - r, hh - r), (-hw + r, hh - r), (-hw + r, -hh + r)]
    P = _rot(pts, cx, cy, deg)
    C = _rot(ctr, cx, cy, deg)
    return [Segment(P[0], P[1]), Segment(P[1], P[2], C[0], True),
            Segment(P[2], P[3]), Segment(P[3], P[4], C[1], True),
            Segment(P[4], P[5]), Segment(P[5], P[6], C[2], True),
            Segment(P[6], P[7]), Segment(P[7], P[0], C[3], True)]


def oval(cx, cy, w, h, deg=0.0):
    if abs(w - h) < EPS:
        return circle(cx, cy, w / 2.0)
    if w > h:
        d = (w - h) / 2.0
        (x1, y1), (x2, y2) = _rot([(-d, 0), (d, 0)], cx, cy, deg)
        return stadium(x1, y1, x2, y2, h)
    d = (h - w) / 2.0
    (x1, y1), (x2, y2) = _rot([(0, -d), (0, d)], cx, cy, deg)
    return stadium(x1, y1, x2, y2, w)


def octagon(cx, cy, w, h, deg=0.0):
    hw, hh = w / 2.0, h / 2.0
    c = min(w, h) / 4.0
    pts = [(-hw + c, -hh), (hw - c, -hh), (hw, -hh + c), (hw, hh - c),
           (hw - c, hh), (-hw + c, hh), (-hw, hh - c), (-hw, -hh + c)]
    return polygon(_rot(pts, cx, cy, deg))


def pad_prim(pad, side, grow=0.0):
    """Primitive for a pad's shape on one side ('top'/'bottom'), grown by `grow` mm."""
    if side == "top":
        (w, h), shape, alt, corner = pad.top_size, pad.top_shape, pad.top_alt, pad.top_corner
    else:
        (w, h), shape, alt, corner = pad.bot_size, pad.bot_shape, pad.bot_alt, pad.bot_corner
    if w <= 0 or h <= 0:
        return None
    w2, h2 = w + 2 * grow, h + 2 * grow
    if w2 <= EPS or h2 <= EPS:
        return None
    x, y, a = pad.x, pad.y, pad.rotation
    if alt == ALT_ROUNDRECT:
        r = corner / 100.0 * min(w, h) / 2.0 + grow
        return ("roundrect", x, y, w2, h2, a, max(r, 0.0))
    if shape == SHAPE_CIRCLE:
        return oval_prim(x, y, w2, h2, a)
    if shape == SHAPE_OCTAGON:
        return ("octagon", x, y, w2, h2, a)
    if grow > EPS:
        return ("roundrect", x, y, w2, h2, a, grow)
    return ("rect", x, y, w2, h2, a)


def oval_prim(cx, cy, w, h, deg=0.0):
    if abs(w - h) < EPS:
        return ("circle", cx, cy, w / 2.0)
    if w > h:
        d = (w - h) / 2.0
        (x1, y1), (x2, y2) = _rot([(-d, 0), (d, 0)], cx, cy, deg)
        return ("stadium", x1, y1, x2, y2, h)
    d = (h - w) / 2.0
    (x1, y1), (x2, y2) = _rot([(0, -d), (0, d)], cx, cy, deg)
    return ("stadium", x1, y1, x2, y2, w)


def shape_prims(shape):
    """Convert a doc.shapes tuple into primitives:
    ("stadium", x1, y1, x2, y2, w) | ("circle", cx, cy, r) |
    ("rect", cx, cy, w, h, deg) | ("roundrect", cx, cy, w, h, deg, r) |
    ("octagon", cx, cy, w, h, deg) | ("poly", outer_segments, [hole_segments])
    """
    kind = shape[0]
    if kind == "track":
        _, x1, y1, x2, y2, w = shape
        return [("stadium", x1, y1, x2, y2, w)]
    if kind == "arc":
        _, cx, cy, r, sa, ea, w = shape
        outer, holes = arc_track(cx, cy, r, sa, ea, w)
        if not holes and len(outer) == 1 and outer[0].center is not None:
            o = outer[0]
            return [("circle", o.center[0], o.center[1], math.dist(o.start, o.center))]
        return [("poly", outer, holes)]
    if kind == "poly":
        return [("poly", shape[1], shape[2])]
    return []


def _pad_on_side(pad, side):
    layer = LAYER_TOP if side == "top" else LAYER_BOTTOM
    return pad.layer == layer or pad.layer == MULTI


def copper_prims(doc, side):
    layer = LAYER_TOP if side == "top" else LAYER_BOTTOM
    out = []
    for s in doc.shapes.get(layer, []):
        out.extend(shape_prims(s))
    for p in doc.pads:
        if _pad_on_side(p, side):
            q = pad_prim(p, side)
            if q:
                out.append(q)
    for v in doc.vias:
        if v.diameter <= 0:
            continue
        if (side == "top" and v.layer_start == LAYER_TOP) or \
           (side == "bottom" and v.layer_end == LAYER_BOTTOM):
            out.append(("circle", v.x, v.y, v.diameter / 2.0))
    return out


def mask_opening_prims(doc, side):
    layer = TOP_SOLDER if side == "top" else BOTTOM_SOLDER
    out = []
    for s in doc.shapes.get(layer, []):
        out.extend(shape_prims(s))
    for p in doc.pads:
        if p.layer == layer:              # pad drawn directly on the mask layer
            q = pad_prim(p, "top")
            if q:
                out.append(q)
            continue
        if not _pad_on_side(p, side):
            continue
        if (side == "top" and p.tent_top) or (side == "bottom" and p.tent_bottom):
            continue
        grow = p.mask_manual if p.mask_mode == 2 else doc.mask_expansion
        q = pad_prim(p, side, grow)
        if q:
            out.append(q)
    for v in doc.vias:
        if (side == "top" and v.tent_top) or (side == "bottom" and v.tent_bottom):
            continue
        if side == "top" and v.layer_start != LAYER_TOP:
            continue
        if side == "bottom" and v.layer_end != LAYER_BOTTOM:
            continue
        grow = v.mask_expansion if (v.mask_manual and v.mask_expansion is not None) \
            else doc.mask_expansion
        r = v.diameter / 2.0 + grow
        if r > 0:
            out.append(("circle", v.x, v.y, r))
    return out


def silk_prims(doc, side):
    layer = TOP_OVERLAY if side == "top" else BOTTOM_OVERLAY
    out = []
    for s in doc.shapes.get(layer, []):
        out.extend(shape_prims(s))
    for p in doc.pads:
        if p.layer == layer:
            q = pad_prim(p, "top")
            if q:
                out.append(q)
    return out


def silk_texts(doc, side):
    layer = TOP_OVERLAY if side == "top" else BOTTOM_OVERLAY
    res = []
    for t in doc.texts:
        if t.layer != layer or not t.text or t.height <= 0:
            continue
        txt = t.text.strip()
        if not txt or txt.startswith(".") or txt.startswith("'."):
            continue  # unresolved special strings such as .Layer_Name
        res.append(t)
    return res


def drill_prims(doc, pads=True, vias=True):
    out = []
    if pads:
        for p in doc.pads:
            if p.hole <= 0:
                continue
            if p.hole_shape == 2 and p.slot_len > p.hole:
                a = math.radians(p.rotation + p.slot_rotation)
                d = (p.slot_len - p.hole) / 2.0
                out.append(("stadium", p.x - d * math.cos(a), p.y - d * math.sin(a),
                            p.x + d * math.cos(a), p.y + d * math.sin(a), p.hole))
            else:
                out.append(("circle", p.x, p.y, p.hole / 2.0))
    if vias:
        for v in doc.vias:
            if v.hole > 0:
                out.append(("circle", v.x, v.y, v.hole / 2.0))
    return out


def prim_center(q):
    if q[0] == "stadium":
        return ((q[1] + q[3]) / 2, (q[2] + q[4]) / 2)
    if q[0] == "poly":
        b = loop_bbox(q[1])
        return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
    return (q[1], q[2])


def loop_bbox(loop):
    xs, ys = [], []
    for s in loop:
        if s.center is not None:
            r = math.dist(s.start, s.center)
            xs += [s.center[0] - r, s.center[0] + r]
            ys += [s.center[1] - r, s.center[1] + r]
        else:
            xs += [s.start[0], s.end[0]]
            ys += [s.start[1], s.end[1]]
    return min(xs), min(ys), max(xs), max(ys)


def clean_loop(loop, tol=5e-4):
    """Drop line edges shorter than tol (mm) and re-join the neighbours.

    Altium polygon pours often contain edges of a few nanometres, which are
    below Fusion's modelling tolerance and make face creation fail. Arcs keep
    their exact endpoints; the adjoining lines are stretched to meet them.
    Returns None if fewer than three edges survive (a sliver).
    """
    if len(loop) <= 1:
        return loop
    segs = list(loop)
    for _ in range(4):
        segs = [s for s in segs if s.center is not None or math.dist(s.start, s.end) >= tol]
        n = len(segs)
        if n == 0:
            return None
        if n == 1 and segs[0].center is not None:
            return segs
        ends = []
        for i, s in enumerate(segs):
            nxt = segs[(i + 1) % n]
            ends.append(s.end if s.center is not None else
                        (nxt.start if nxt.center is not None else s.end))
        out = []
        for i, s in enumerate(segs):
            if s.center is not None:
                prev = segs[i - 1]
                if prev.center is not None and prev.end != s.start:
                    # two arcs left touching after a sliver line was dropped
                    s = Segment(prev.end, s.end, s.center, s.ccw)
                out.append(s)
            else:
                out.append(Segment(ends[i - 1], ends[i]))
        stable = all(s.center is not None or math.dist(s.start, s.end) >= tol for s in out)
        segs = out
        if stable:
            break
    if len(segs) < 3 and not any(s.center is not None for s in segs):
        return None
    return _drop_collinear(segs, tol * 2)


def _drop_collinear(segs, tol):
    """Merge consecutive line edges whose shared vertex lies within tol of
    the chord. Pours store arcs as dense polylines, and every extra edge
    makes the Fusion booleans slower."""
    if len(segs) < 4 or any(s.center is not None for s in segs):
        return segs
    pts = [s.start for s in segs]
    changed = True
    while changed and len(pts) > 3:
        changed = False
        # merge vertices that a dropped spike left on top of each other
        dedup = [pts[0]]
        for p in pts[1:]:
            if math.dist(p, dedup[-1]) >= tol:
                dedup.append(p)
        if len(dedup) > 1 and math.dist(dedup[0], dedup[-1]) < tol:
            dedup.pop()
        if len(dedup) != len(pts):
            changed = True
        pts = dedup
        if len(pts) <= 3:
            break
        n = len(pts)
        keep = [True] * n
        for i in range(n):
            if not keep[i - 1]:
                continue           # never drop two neighbours in one pass
            a, b, c = pts[i - 1], pts[i], pts[(i + 1) % n]
            dx, dy = c[0] - a[0], c[1] - a[1]
            L = math.hypot(dx, dy)
            if L < tol:
                keep[i] = False    # zero-width spike: out to b and straight back
                changed = True
                continue
            if abs((b[0] - a[0]) * dy - (b[1] - a[1]) * dx) / L < tol:
                keep[i] = False
                changed = True
        if sum(keep) < 3:
            break
        pts = [p for p, k in zip(pts, keep) if k]
    n = len(pts)
    return [Segment(pts[i], pts[(i + 1) % n]) for i in range(n)]
