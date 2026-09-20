// Pure hue-assignment for board chips shared by board.js's card-face chips
// (assignee, tag) and the drawer's tag-chip picker. Owns no DOM — callers
// build the ordered name list (assignees first in their fixed order, then
// every other distinct tag) and apply the returned hue as a `--chip-hue`
// inline style; the CSS derives the actual color from that custom property.

// Spaces hues evenly around the wheel (360 / distinct-name-count), in the
// order names first appear. A later duplicate of an earlier name is
// dropped before spacing is computed, so a repeated name never narrows the
// gap between the others. An empty list returns an empty map.
export function assignChipHues(names) {
  const ordered = [];
  const seen = new Set();
  for (const name of (names || [])) {
    if (seen.has(name)) continue;
    seen.add(name);
    ordered.push(name);
  }
  const map = new Map();
  const count = ordered.length;
  ordered.forEach((name, index) => {
    map.set(name, Math.round((360 / count) * index));
  });
  return map;
}
