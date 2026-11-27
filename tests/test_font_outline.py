"""Checks for font_outline (pure Python, no Fusion).

Run: python tests/test_font_outline.py [font files...]

Without arguments it tries a few common TrueType (glyf) and OpenType CFF
fonts on Windows and Linux and skips the ones that aren't there.
"""
import glob
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import font_outline as fo  # noqa: E402


def square(x0, y0, x1, y1, ccw=True):
    pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return pts if ccw else pts[::-1]


def test_classify():
    # 'o' drawn either way round: outer + opposite-wound counter = one hole
    for ccw in (True, False):
        shapes = fo.classify([square(0, 0, 10, 10, ccw), square(3, 3, 7, 7, not ccw)])
        assert len(shapes) == 1 and len(shapes[0][1]) == 1, shapes
    # overlapping strokes wound the same way stay filled (nonzero rule)
    shapes = fo.classify([square(0, 0, 10, 2), square(4, -3, 6, 5)])
    assert len(shapes) == 2 and not shapes[0][1] and not shapes[1][1], shapes
    # a stroke drawn entirely inside another, same direction: still filled
    shapes = fo.classify([square(0, 0, 10, 10), square(3, 3, 7, 7)])
    assert len(shapes) == 2 and not any(h for _, h in shapes), shapes
    # island inside a counter ('®'-like): outer, hole, island
    shapes = fo.classify([square(0, 0, 10, 10), square(2, 2, 8, 8, False), square(4, 4, 6, 6)])
    assert len(shapes) == 2 and sum(len(h) for _, h in shapes) == 1, shapes


def check_font(path):
    f = fo.Font(path)
    assert f.cell > 0 and f.upem > 0
    glyphs = fo.text_shapes(f, "Klon 8o", 5.08)
    holes = [sum(len(h) for _, h in g) for g in glyphs]
    # K l o n 8 o -> the 'o's have one counter, the '8' two
    assert len(glyphs) == 6, (path, len(glyphs))
    assert holes[2] == 1 and holes[4] == 2 and holes[5] == 1, (path, holes)
    xs = [p[0] for g in glyphs for o, _ in g for p in o]
    ys = [p[1] for g in glyphs for o, _ in g for p in o]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    assert 2 < h < 5.08 and 5 < w < 30, (path, w, h)
    # mirrored text runs leftwards from x=0
    mx = [p[0] for g in fo.text_shapes(f, "Klon", 5.08, mirrored=True) for o, _ in g for p in o]
    assert max(mx) < 0.5 < -min(mx), path   # (italics may overhang x=0 a little)
    print("ok  %-40s %s  %.2f x %.2f mm" % (os.path.basename(path), f.kind, w, h))


def main():
    test_classify()
    print("ok  classify")
    paths = sys.argv[1:] or [p for pat in (
        r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\calibri.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/opentype/*/*.otf",
        "/usr/share/fonts/opentype/*/*/*.otf") for p in sorted(glob.glob(pat))[:3]]
    if not paths:
        print("skip font files: none found")
    for p in paths:
        check_font(p)


if __name__ == "__main__":
    main()
