// Pure board ordering helpers. Keeping these outside the DOM renderer makes
// mixed task/schedule ordering and direction reversal easy to verify without
// a browser.

export function normalizedSortKey(card, mode) {
  if (mode.startsWith('created')) {
    const raw = card.created_at || card.created_date || card.next_fire_at || '';
    const timestamp = raw ? Date.parse(raw) : NaN;
    return {
      missing: !Number.isFinite(timestamp),
      value: Number.isFinite(timestamp) ? timestamp : 0,
    };
  }
  if (mode.startsWith('modified')) {
    const raw = card.updated_at || (card.last_run && card.last_run.at) || card.next_fire_at || '';
    const timestamp = raw ? Date.parse(raw) : NaN;
    return {
      missing: !Number.isFinite(timestamp),
      value: Number.isFinite(timestamp) ? timestamp : 0,
    };
  }
  if (mode === 'assignee_asc') {
    if (card.kind === 'schedule') return { missing: false, value: '\uffff' };
    return { missing: false, value: (card.assignee || '\ufffe').toLowerCase() };
  }
  return { missing: false, value: 0 };
}

export function compareSortValues(a, b) {
  if (a === b) return 0;
  return a < b ? -1 : 1;
}

export function sortCards(cards, mode) {
  if (!mode || mode === 'file') return cards;
  const descending = mode.endsWith('_desc');
  return cards
    .map(card => ({
      card,
      key: normalizedSortKey(card, mode),
      // The id tie-breaker makes equal/missing timestamps deterministic and
      // reverses exactly with the selected direction.
      tie: `${card.kind || ''}:${card.id || ''}`,
    }))
    .sort((a, b) => {
      const missingComparison = Number(a.key.missing) - Number(b.key.missing);
      if (missingComparison) return descending ? -missingComparison : missingComparison;
      const valueComparison = compareSortValues(a.key.value, b.key.value);
      if (valueComparison) return descending ? -valueComparison : valueComparison;
      const tieComparison = compareSortValues(a.tie, b.tie);
      return descending ? -tieComparison : tieComparison;
    })
    .map(({ card }) => card);
}

export function loadSortSelection(storage, key, options, defaultSort) {
  try {
    const value = storage.getItem(key);
    if (value && options.has(value)) return value;
  } catch (_) {}
  return defaultSort;
}

export function saveSortSelection(storage, key, value) {
  try { storage.setItem(key, value); } catch (_) {}
}
