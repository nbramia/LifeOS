// web/agents/lanes.js
//
// The board's lane table and its colour palette — the single source of
// truth `web/agents/board.js` (lane columns) and `web/agents/graph.js`
// (node fill colour + colour legend) both import, so the two views agree on
// which lanes exist, their order, and what each one looks like. Mirrors
// `api/services/agent_board.py`'s `LANES` tuple (id/order only — the labels
// and colours are display concerns that stay client-side).

export const LANES = [
  { id: 'unassigned',  label: 'Unassigned' },
  { id: 'assigned',    label: 'Assigned' },
  { id: 'in_progress', label: 'In progress' },
  { id: 'human_queue', label: 'Human queue' },
  { id: 'scheduled',   label: 'Scheduled' },
  { id: 'review',      label: 'Review' },
  { id: 'done',        label: 'Done' },
];

// Okabe-Ito colour-blind-safe palette (minus black, which doesn't read on
// the dark background) — seven distinct, legible-on-dark hues, one per lane.
export const LANE_COLORS = {
  unassigned:  '#56B4E9',
  assigned:    '#0072B2',
  in_progress: '#009E73',
  human_queue: '#D55E00',
  scheduled:   '#CC79A7',
  review:      '#F0E442',
  done:        '#9a9aa8',
};

export function laneColor(lane) {
  return LANE_COLORS[lane] || LANE_COLORS.unassigned;
}
