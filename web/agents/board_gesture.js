// Pure pointer-gesture decisions shared by the board's DOM handlers and
// focused non-browser tests. Touch starts only after a hold; before that,
// native lane/page scrolling owns the gesture.

export const POINTER_SLOP = 8;
export const TOUCH_HOLD_MS = 260;

export function pointerIsActive(state, event) {
  return !!state && event.isPrimary !== false && event.pointerId === state.pointerId;
}

export function shouldCancelPointerGesture(state, dx, dy) {
  if (Math.hypot(dx, dy) < POINTER_SLOP) return false;
  if (state.pointerType === 'touch' && !state.holdReady) return true;
  const vertical = Math.abs(dy) > Math.abs(dx);
  return vertical && (state.pointerType === 'touch' || state.kind === 'card');
}
