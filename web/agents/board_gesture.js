// Pure pointer-gesture decisions shared by the board's DOM handlers and
// focused non-browser tests. Touch never drags: the lane strip scrolls
// horizontally on a phone, and a custom drag can only own that axis by
// taking it away from that scroller. Mouse and pen keep the drag, where
// horizontal movement costs no scrolling gesture.

export const POINTER_SLOP = 8;

export function pointerCanDrag(pointerType) {
  return pointerType !== 'touch';
}

export function pointerIsActive(state, event) {
  return !!state && event.isPrimary !== false && event.pointerId === state.pointerId;
}

export function shouldCancelPointerGesture(state, dx, dy) {
  if (Math.hypot(dx, dy) < POINTER_SLOP) return false;
  return Math.abs(dy) > Math.abs(dx) && state.kind === 'card';
}
