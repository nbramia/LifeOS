// Preserve the original action failure while best-effort refreshing the
// caller's view of the card.

export async function refreshAfterFailure(onChanged) {
  if (!onChanged) return;
  try { await onChanged(); } catch (_) {}
}
