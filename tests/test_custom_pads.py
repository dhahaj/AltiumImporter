"""Checks for custom-shaped pads (pure Python, no Fusion).

Run: python tests/test_custom_pads.py
"""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from altium_pcbdoc import Pad, Segment  # noqa: E402
import layer_geometry as lg  # noqa: E402


class Doc:
    def __init__(self, pads):
        self.pads, self.vias, self.shapes = pads, [], {}
        self.mask_expansion = 0.1


def custom_pad(x, y, outline):
    p = Pad()
    p.x, p.y, p.rotation, p.layer = x, y, 0.0, lg.LAYER_BOTTOM
    p.top_size = p.bot_size = p.size = (0.0254, 0.0254)
    p.top_shape = p.bot_shape = lg.SHAPE_CIRCLE
    p.top_alt = p.bot_alt = p.top_corner = p.bot_corner = 0
    p.hole, p.mask_mode, p.mask_manual = 0.0, 1, 0.0
    p.tent_top = p.tent_bottom = False
    p.custom = {lg.LAYER_BOTTOM: [(outline, [])]}
    return p


def c_shape():
    """Horseshoe: outer arc r=1 (ccw, 30..330 deg), inner arc r=0.6 back (cw)."""
    def pt(r, a):
        return (r * math.cos(math.radians(a)), r * math.sin(math.radians(a)))
    return [Segment(pt(1, 30), pt(1, 330), (0, 0), True),
            Segment(pt(1, 330), pt(0.6, 330)),
            Segment(pt(0.6, 330), pt(0.6, 30), (0, 0), False),
            Segment(pt(0.6, 30), pt(1, 30))]


def covers(q, x, y):
    """Point-in-primitive for the shapes grown_prims emits (bbox for polys)."""
    if q[0] == "stadium":
        _, x1, y1, x2, y2, w = q
        dx, dy = x2 - x1, y2 - y1
        L2 = dx * dx + dy * dy
        t = 0 if L2 == 0 else max(0, min(1, ((x - x1) * dx + (y - y1) * dy) / L2))
        return math.dist((x, y), (x1 + t * dx, y1 + t * dy)) <= w / 2 + 1e-9
    if q[0] == "poly":
        b = lg.loop_bbox(q[1])
        return b[0] - 1e-9 <= x <= b[2] + 1e-9 and b[1] - 1e-9 <= y <= b[3] + 1e-9
    raise AssertionError(q[0])


def main():
    outline = c_shape()
    doc = Doc([custom_pad(0.8, 0.0, outline)])

    cu = lg.copper_prims(doc, "bottom")
    assert len(cu) == 1 and cu[0][0] == "poly", cu
    assert lg.copper_prims(doc, "top") == []
    print("ok  copper uses the custom outline, not the 1 mil pad")

    mask = lg.mask_opening_prims(doc, "bottom")
    assert len(mask) == 1 + len(outline), len(mask)
    # 0.1 mm outside the outer arc at 180 deg, and inside the mouth near the inner arc
    for x, y in ((-1.09, 0.0), (0.0, -1.09), (-0.51, 0.0)):
        assert any(covers(q, x, y) for q in mask[1:]), (x, y)
    print("ok  mask opening = outline grown by the expansion (%d prims)" % len(mask))

    doc.pads[0].tent_bottom = True
    assert lg.mask_opening_prims(doc, "bottom") == []
    print("ok  tented custom pad gets no mask opening")


if __name__ == "__main__":
    main()
