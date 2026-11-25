"""Checks for layer_geometry.split_self_touching (pure Python, no Fusion).

Run: python tests/test_self_touching.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from altium_pcbdoc import Segment  # noqa: E402
import layer_geometry as lg  # noqa: E402


def loop(pts):
    return [Segment(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))]


def area(segs):
    return abs(lg._area2([s.start for s in segs])) / 2


def filled(parts):
    return sum(area(o) - sum(area(h) for h in hs) for o, hs in parts)


def simple(segs):
    pts = [s.start for s in segs]
    return lg._simple_loops(pts, lg.TOUCH_TOL) is None


def check(name, outer, holes, n_parts, want_area):
    parts = lg.split_self_touching(loop(outer), [loop(h) for h in holes])
    got = filled(parts)
    assert len(parts) == n_parts, "%s: %d parts, want %d" % (name, len(parts), n_parts)
    assert abs(got - want_area) < 1e-9, "%s: area %g, want %g" % (name, got, want_area)
    for o, hs in parts:
        assert simple(o) and all(simple(h) for h in hs), "%s: loop still touches itself" % name
    print("ok  %-28s %d part(s), area %g" % (name, len(parts), got))


# a plain square is returned untouched
sq = [(0, 0), (0, 1), (1, 1), (1, 0)]
parts = lg.split_self_touching(loop(sq), [])
assert len(parts) == 1 and parts[0][0][0].start == (0, 0)
print("ok  simple square untouched")

# two squares sharing one corner, drawn as one clockwise outline
check("corner touch", [(0, 0), (0, 1), (1, 1), (1, 2), (2, 2), (2, 1), (1, 1), (1, 0)],
      [], 2, 2.0)

# a zero-width slit: two letters joined by a hairline that runs out and back
check("slit", [(0, 0), (0, 1), (1, 1), (1, 0.5), (2, 0.5), (2, 1), (3, 1), (3, 0),
               (2, 0), (2, 0.5), (1, 0.5), (1, 0)], [], 2, 2.0)

# a finger resting on the far edge (the DETEX case): the outline runs back
# along part of its own bottom edge
check("edge overlap", [(0, 0), (0, 2), (3, 2), (3, 0), (2, 0), (2, 1), (1.5, 1),
                       (1.5, 0)], [], 1, 6.0 - 0.5)

# a void pinched onto the outline at one corner becomes a hole
check("void on a slit", [(0, 0), (0, 3), (3, 3), (3, 0), (2, 0), (2, 1), (2, 2),
                         (1, 2), (1, 1), (2, 1), (2, 0)], [], 1, 9.0 - 1.0)

# a bowtie: both lobes are filled
check("bowtie", [(0, 0), (0, 2), (2, 0), (2, 2)], [], 2, 2.0)

# an ordinary hole stays with the piece that surrounds it
check("hole kept", [(0, 0), (0, 3), (3, 3), (3, 0), (4, 0), (4, -1), (3, -1), (3, 0)],
      [[(1, 1), (2, 1), (2, 2), (1, 2)]], 2, 9.0 - 1.0 + 1.0)
print("all passed")
