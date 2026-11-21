// Checks the colour picker's preview board for overlaps:
//   node tests/check_picker_layout.js   (needs Playwright)
// Labels must not touch pads, vias, holes, traces, other labels or other
// parts' outlines; part outlines, vias and holes must not touch each other;
// traces must stay off labels and mounting holes.
const path = require('path');
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 300, height: 320 } });
  await p.goto('file://' + path.resolve(__dirname, '..', 'html', 'color_picker.html'));
  const res = await p.evaluate(() => {
    const svg = document.getElementById('board');
    const items = [...svg.querySelectorAll('[data-k]')].map((e, i) => {
      const bb = e.getBBox();
      return { i, k: e.dataset.k, el: e, x0: bb.x, y0: bb.y, x1: bb.x + bb.width, y1: bb.y + bb.height,
               t: e.textContent || '' };
    });
    const hit = (a, b, m) => a.x0 - m < b.x1 && b.x0 - m < a.x1 && a.y0 - m < b.y1 && b.y0 - m < a.y1;
    const inside = (a, b) => a.x0 >= b.x0 && a.x1 <= b.x1 && a.y0 >= b.y0 && a.y1 <= b.y1;
    const bad = [];
    const by = k => items.filter(it => it.k === k);
    const name = it => it.k + (it.t ? '"' + it.t + '"' : '') + '@' + [it.x0, it.y0].map(Math.round);
    for (const t of by('text')) {
      for (const o of items) {
        if (o === t || o.k === 'trace') continue;
        if (o.k === 'silk' && inside(t, o)) continue;          // label inside its own outline (U1)
        if (o.k === 'pad' || o.k === 'via' || o.k === 'hole' || o.k === 'text' || o.k === 'silk')
          if (hit(t, o, 1)) bad.push(name(t) + ' x ' + name(o));
      }
    }
    const solid = items.filter(it => ['silk', 'via', 'hole'].includes(it.k));
    for (let a = 0; a < solid.length; a++) for (let c = a + 1; c < solid.length; c++)
      if (hit(solid[a], solid[c], 1)) bad.push(name(solid[a]) + ' x ' + name(solid[c]));
    // traces: sample points along each path
    const avoid = items.filter(it => it.k === 'text' || it.k === 'hole');
    for (const tr of by('trace')) {
      const L = tr.el.getTotalLength();
      for (let s = 0; s <= L; s += 1) {
        const q = tr.el.getPointAtLength(s);
        const pt = { x0: q.x, y0: q.y, x1: q.x, y1: q.y };
        for (const o of avoid) if (hit(pt, o, 2)) { bad.push('trace ' + tr.el.getAttribute('d') + ' x ' + name(o)); break; }
      }
    }
    // everything stays on the board, clear of the edge
    for (const it of items) if (it.x0 < 3 || it.y0 < 3 || it.x1 > 277 || it.y1 > 109) bad.push(name(it) + ' off board');
    return [...new Set(bad)];
  });
  await b.close();
  if (res.length) { console.log('OVERLAPS:\n  ' + res.join('\n  ')); process.exit(1); }
  console.log('preview layout OK');
})();
