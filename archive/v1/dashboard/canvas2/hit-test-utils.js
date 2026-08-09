// Canvas v2 — shared hit-test helper. A pure, synchronous point→token lookup shared by every
// gesture that needs "what token is under the pointer" (press-to-select/drag, context menu, the
// empty-floor placement check, …), so none of them reimplement the resolution on their own.
//
// The overlay layer (.lw2-overlay: connection handles, marquee, rubber-band) paints ON TOP of
// tokens in the SVG stacking order — the same shape as the bug that broke v1's Konva hit graph,
// where a "listening" decoration shape sitting over the real target ate the click. Rather than
// stopping at the single topmost element (e.target), this walks the FULL elementsFromPoint stack,
// topmost first, and skips anything that isn't part of a real token — so a click still resolves
// to the token underneath even if a non-token overlay shape happens to be painted above it at
// that pixel. It reads the already-rendered DOM as-is: no extra render/redraw pass, no state.
export function hitTokenAt(clientX, clientY) {
  const stack = (document.elementsFromPoint && document.elementsFromPoint(clientX, clientY)) || [];
  for (const el of stack) {
    const tok = el.closest && el.closest(".lw2-token");
    if (tok) return tok;
  }
  return null;
}
