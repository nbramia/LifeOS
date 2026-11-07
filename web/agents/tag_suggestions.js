// web/agents/tag_suggestions.js
//
// Suggested tags for a task card, computed entirely from what the board
// already holds — no network call. Pure (no DOM), so it is directly
// unit-testable; web/agents/board.js renders the result as the drawer's
// faded suggestion chips.
//
// Each candidate tag (any tag on another task card that `normalizeTag`
// accepts) scores the sum of:
//   * name match — the fraction of the tag's words that appear among the
//     card's own title/notes words (`docs` in "Update the docs" scores 1,
//     `home-repair` against "repair the gate" scores 0.5);
//   * title similarity — for every other task card carrying the tag, the
//     Jaccard overlap of that card's title words with this card's title
//     words (stopwords removed).
// Tags already on the card, tags `normalizeTag` rejects (assignee and
// lifecycle tags, in the drawer's case), and zero-score tags never appear.

const STOPWORDS = new Set([
  'a', 'an', 'and', 'are', 'as', 'at', 'be', 'by', 'for', 'from', 'in', 'into',
  'is', 'it', 'its', 'of', 'on', 'or', 'the', 'to', 'up', 'with', 'my', 'me',
  'i', 'we', 'our', 'this', 'that', 'do', 'get', 'new', 'about', 'out',
]);

export function wordsOf(text) {
  return String(text || '')
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter(word => word.length > 1 && !STOPWORDS.has(word));
}

function jaccard(left, right) {
  if (!left.size || !right.size) return 0;
  let shared = 0;
  for (const word of left) if (right.has(word)) shared += 1;
  return shared / (left.size + right.size - shared);
}

// `card`: `{ id, title, notes, tags }`. `cards`: every task card on the
// board (`card` itself is skipped by id). `normalizeTag(raw)` returns the
// canonical editable tag or null for one that must never be suggested.
export function suggestTags(card, cards, { normalizeTag = (t) => t, limit = 3 } = {}) {
  const own = new Set((card.tags || []).map(tag => normalizeTag(tag)).filter(Boolean));
  const cardWords = new Set([...wordsOf(card.title), ...wordsOf(card.notes)]);
  const titleWords = new Set(wordsOf(card.title));
  const scores = new Map();
  const add = (tag, amount) => scores.set(tag, (scores.get(tag) || 0) + amount);

  for (const other of cards || []) {
    if (!other || other.id === card.id) continue;
    const tags = [...new Set((other.tags || []).map(tag => normalizeTag(tag)).filter(Boolean))]
      .filter(tag => !own.has(tag));
    if (!tags.length) continue;
    const similarity = jaccard(titleWords, new Set(wordsOf(other.title)));
    for (const tag of tags) add(tag, similarity);
  }

  for (const tag of scores.keys()) {
    const tagWords = tag.split(/[-_]+/).filter(Boolean);
    const found = tagWords.filter(word => cardWords.has(word)).length;
    if (tagWords.length) add(tag, found / tagWords.length);
  }

  return [...scores.entries()]
    .filter(([, score]) => score > 0)
    .sort((a, b) => (b[1] - a[1]) || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0))
    .slice(0, limit)
    .map(([tag]) => tag);
}
