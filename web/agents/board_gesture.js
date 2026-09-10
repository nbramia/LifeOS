// Pure pointer-gesture decisions shared by the board's DOM handlers and
// focused non-browser tests. CSS reserves the horizontal axis for the
// custom drag (`touch-action: pan-y` on draggable sources), while vertical
// movement remains native page/lane scrolling.

export const POINTER_SLOP = 8;

export function pointerIsActive(state, event) {
  return !!state && event.isPrimary !== false && event.pointerId === state.pointerId;
}

export function shouldCancelPointerGesture(state, dx, dy) {
  if (Math.hypot(dx, dy) < POINTER_SLOP) return false;
  const vertical = Math.abs(dy) > Math.abs(dx);
  return vertical && (state.pointerType === 'touch' || state.kind === 'card');
}
