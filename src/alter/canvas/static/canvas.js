/* ═══════════════════════════════════════════════════════════════════════════
   Alter ERD Canvas — phase-2c
   Preview mode, migration sidebar, editing modals, paste SQL, templates,
   undo/redo, context menu — built on top of the 2b rendering engine.
   ═══════════════════════════════════════════════════════════════════════════ */

'use strict';

// ─── Constants ────────────────────────────────────────────────────────────────

const CARD_W   = 248;
const HEADER_H = 34;
const ROW_H    = 28;

const MINI_W   = 192;
const MINI_H   = 132;
const MINI_PAD = 10;

// ─── State ────────────────────────────────────────────────────────────────────

let schema = null;           // full response from /api/schema (includes proposed_schema, changes)
let selectedTables = new Set();
let dragging     = null;
const tableEls   = {};       // table_name → .table-card DOM element
let pz           = null;
let _sseSource   = null;     // EventSource for SSE live-sync

// ─── Type display ─────────────────────────────────────────────────────────────

const TYPE_LABELS = {
  uuid:     'uuid',
  string:   'varchar',
  text:     'text',
  int:      'int',
  bigint:   'bigint',
  float:    'float',
  decimal:  'numeric',
  bool:     'bool',
  datetime: 'timestamptz',
  date:     'date',
  time:     'time',
  json:     'jsonb',
  bytes:    'bytea',
};

function typeLabel(col) {
  if (col.type === 'string' && col.max_length) return `varchar(${col.max_length})`;
  return TYPE_LABELS[col.type] ?? col.type;
}

const BUILTIN_TYPES = [
  { value: 'uuid',     label: 'uuid' },
  { value: 'string',   label: 'string (varchar)' },
  { value: 'text',     label: 'text' },
  { value: 'int',      label: 'int' },
  { value: 'bigint',   label: 'bigint' },
  { value: 'float',    label: 'float' },
  { value: 'decimal',  label: 'decimal (numeric)' },
  { value: 'bool',     label: 'bool' },
  { value: 'datetime', label: 'datetime (timestamptz)' },
  { value: 'date',     label: 'date' },
  { value: 'json',     label: 'json (jsonb)' },
];

function populateTypeSelect(sel, currentVal) {
  sel.innerHTML = '';
  const builtinGroup = document.createElement('optgroup');
  builtinGroup.label = 'Built-in';
  for (const t of BUILTIN_TYPES) {
    const opt = document.createElement('option');
    opt.value = t.value;
    opt.textContent = t.label;
    builtinGroup.appendChild(opt);
  }
  sel.appendChild(builtinGroup);

  const enums = schema ? effectiveEnums() : [];
  if (enums.length > 0) {
    const enumGroup = document.createElement('optgroup');
    enumGroup.label = 'Enums';
    for (const e of enums) {
      const opt = document.createElement('option');
      opt.value = e.name;
      opt.textContent = e.name;
      enumGroup.appendChild(opt);
    }
    sel.appendChild(enumGroup);
  }

  if (currentVal !== undefined) {
    sel.value = currentVal;
    // If the value wasn't matched (e.g. unknown type), fall back to 'string'
    if (!sel.value) sel.value = 'string';
  }
}

function getCardHeight(table) {
  return HEADER_H + (table.columns ?? []).length * ROW_H;
}

// ─── Init ─────────────────────────────────────────────────────────────────────

async function init() {
  try {
    schema = await fetch('/api/schema').then(r => r.json());
  } catch (e) {
    console.error('Failed to load schema:', e);
    return;
  }

  updateToolbarMeta();
  updatePendingUI();
  initPanzoom();

  if (schema.layout_auto) {
    await runElkLayout();
  }

  renderAll();

  requestAnimationFrame(() => {
    renderRelations();
    updateHighlights();
    updateMinimap();
    fitToScreen();
  });

  // Start SSE live-sync after the initial render.
  setupSSE();
  // Check for untracked/unmapped tables (non-blocking).
  checkAwareness();
}

// Re-fetch schema and refresh the full canvas (used after propose/commit/etc.)
async function refreshSchema() {
  try {
    schema = await fetch('/api/schema').then(r => r.json());
  } catch (e) {
    console.error('Failed to refresh schema:', e);
    return;
  }
  updateToolbarMeta();
  updatePendingUI();
  renderAll();
  requestAnimationFrame(() => {
    renderRelations();
    updateHighlights();
    updateMinimap();
  });
  refreshSidebar();
}

// ─── Toolbar ──────────────────────────────────────────────────────────────────

function updateToolbarMeta() {
  const tables = effectiveTables();
  const count = tables.length;
  document.getElementById('table-count').textContent =
    `${count} table${count !== 1 ? 's' : ''}`;
  document.getElementById('schema-name').textContent =
    schema.metadata?.sqlmodel_module ?? 'schema.alter';
}

function effectiveTables() {
  if (schema.proposed_schema) return schema.proposed_schema.tables ?? [];
  return schema.tables ?? [];
}

function effectiveRelations() {
  if (schema.proposed_schema) return schema.proposed_schema.relations ?? [];
  return schema.relations ?? [];
}

function effectiveEnums() {
  if (schema.proposed_schema) return schema.proposed_schema.enums ?? [];
  return schema.enums ?? [];
}

function updatePendingUI() {
  const pendingGroup = document.getElementById('pending-group');
  const badge = document.getElementById('pending-badge');
  const n = schema.pending_count ?? 0;
  if (n > 0) {
    badge.textContent = `${n} pending change${n !== 1 ? 's' : ''}`;
    pendingGroup.hidden = false;
  } else {
    pendingGroup.hidden = true;
  }
  const tables = effectiveTables();
  document.getElementById('empty-state').hidden = tables.length > 0;
}

// ─── panzoom ──────────────────────────────────────────────────────────────────

function initPanzoom() {
  const canvas = document.getElementById('canvas');
  pz = panzoom(canvas, {
    maxZoom:      3,
    minZoom:      0.12,
    bounds:       false,
    smoothScroll: false,
    filterKey:    () => true,
    onTouch:      () => false,
  });
  pz.on('transform', updateMinimap);

  document.getElementById('btn-zoom-in').addEventListener('click', () => {
    const wrap = document.getElementById('canvas-wrap');
    pz.smoothZoom(wrap.clientWidth / 2, wrap.clientHeight / 2, 1.4);
  });
  document.getElementById('btn-zoom-out').addEventListener('click', () => {
    const wrap = document.getElementById('canvas-wrap');
    pz.smoothZoom(wrap.clientWidth / 2, wrap.clientHeight / 2, 1 / 1.4);
  });
  document.getElementById('btn-zoom-fit').addEventListener('click', fitToScreen);
}

function fitToScreen() {
  const tables = effectiveTables();
  if (!tables.length || !pz) return;

  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const t of tables) {
    const x = t.position?.x ?? 0;
    const y = t.position?.y ?? 0;
    const h = getCardHeight(t);
    minX = Math.min(minX, x); minY = Math.min(minY, y);
    maxX = Math.max(maxX, x + CARD_W); maxY = Math.max(maxY, y + h);
  }
  if (!isFinite(minX)) return;

  const PAD  = 60;
  const wrap = document.getElementById('canvas-wrap');
  const vW   = wrap.clientWidth  || window.innerWidth;
  const vH   = wrap.clientHeight || (window.innerHeight - 44);
  const contentW = (maxX - minX) + PAD * 2;
  const contentH = (maxY - minY) + PAD * 2;
  if (contentW <= 0 || contentH <= 0) return;

  const scale = Math.min(vW / contentW, vH / contentH, 1.5);
  const tx = (vW - (maxX - minX) * scale) / 2 - minX * scale + PAD * scale;
  const ty = (vH - (maxY - minY) * scale) / 2 - minY * scale + PAD * scale;
  pz.zoomAbs(0, 0, scale);
  pz.moveTo(tx, ty);
}

// ─── ELK auto-layout ──────────────────────────────────────────────────────────

async function runElkLayout() {
  if (typeof ELK === 'undefined') return;
  const elk = new ELK();
  const tables = effectiveTables();
  const relations = effectiveRelations();
  const graph = {
    id: 'root',
    layoutOptions: {
      'elk.algorithm':                              'layered',
      'elk.direction':                              'RIGHT',
      'elk.spacing.nodeNode':                       '60',
      'elk.layered.spacing.edgeNodeBetweenLayers':  '80',
      'elk.padding':                                '[top=80,left=80,bottom=80,right=80]',
      'elk.layered.crossingMinimization.strategy':  'LAYER_SWEEP',
    },
    children: tables.map(t => ({ id: t.name, width: CARD_W, height: getCardHeight(t) })),
    edges: (relations ?? [])
      .filter(r => r.from_table !== r.to_table)
      .map(r => ({ id: r.name, sources: [r.from_table], targets: [r.to_table] })),
  };

  try {
    const result = await elk.layout(graph);
    for (const node of (result.children ?? [])) {
      const table = tables.find(t => t.name === node.id);
      if (table) table.position = { x: Math.round(node.x), y: Math.round(node.y) };
    }
    for (const table of tables) {
      fetch('/api/position', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ table: table.name, x: table.position.x, y: table.position.y }),
      }).catch(() => {});
    }
  } catch (err) {
    console.warn('ELK layout failed:', err);
  }
}

// ─── Render all ───────────────────────────────────────────────────────────────

function renderAll() {
  renderTables();
  renderRelations();
  updateHighlights();
  updateMinimap();
}

// ─── Table card rendering ─────────────────────────────────────────────────────

function renderTables() {
  const canvas = document.getElementById('canvas');
  for (const el of canvas.querySelectorAll('.table-card')) el.remove();
  Object.keys(tableEls).forEach(k => delete tableEls[k]);

  const tables = effectiveTables();
  const changeIndex = buildChangeIndex();

  for (const table of tables) {
    const card = buildTableCard(table, changeIndex);
    canvas.appendChild(card);
    tableEls[table.name] = card;
  }

  // Render removed tables from original schema (not present in proposed)
  if (schema.proposed_schema) {
    for (const removedName of (changeIndex.removedTables ?? new Set())) {
      const origTable = (schema.tables ?? []).find(t => t.name === removedName);
      if (origTable && !tableEls[removedName]) {
        const card = buildTableCard(origTable, changeIndex);
        canvas.appendChild(card);
        tableEls[removedName] = card;
      }
    }
  }
}

function buildChangeIndex() {
  const idx = {
    addedTables:   new Set(),
    removedTables: new Set(),
    addedCols:     {},
    removedCols:   {},
    modifiedCols:  {},
  };
  if (!schema.changes) return idx;
  for (const ch of schema.changes) {
    if (ch.type === 'add_table')     idx.addedTables.add(ch.table);
    if (ch.type === 'drop_table')    idx.removedTables.add(ch.table);
    if (ch.type === 'add_column')    { (idx.addedCols[ch.table]    ??= new Set()).add(ch.column); }
    if (ch.type === 'drop_column')   { (idx.removedCols[ch.table]  ??= new Set()).add(ch.column); }
    if (ch.type === 'modify_column') { (idx.modifiedCols[ch.table] ??= new Set()).add(ch.column); }
  }
  return idx;
}

function buildTableCard(table, changeIndex = {}) {
  const card = document.createElement('div');
  card.className = 'table-card';
  card.dataset.table = table.name;
  card.style.left = (table.position?.x ?? 80) + 'px';
  card.style.top  = (table.position?.y ?? 80) + 'px';

  if (changeIndex.addedTables?.has(table.name))   card.classList.add('preview-new');
  if (changeIndex.removedTables?.has(table.name)) card.classList.add('preview-removed');

  // ── Header
  const header = document.createElement('div');
  header.className = 'tbl-header';
  const nameSpan  = document.createElement('span');
  nameSpan.className   = 'tbl-name';
  nameSpan.textContent = table.name;
  const countSpan = document.createElement('span');
  countSpan.className   = 'tbl-col-count';
  countSpan.textContent = `${(table.columns ?? []).length}`;
  header.appendChild(nameSpan);
  header.appendChild(countSpan);
  card.appendChild(header);

  // ── Column rows
  const body = document.createElement('div');
  body.className = 'tbl-body';
  for (const col of (table.columns ?? [])) {
    const row = buildColRow(col, table.name);
    if (changeIndex.addedCols?.[table.name]?.has(col.name))    row.classList.add('preview-new');
    if (changeIndex.modifiedCols?.[table.name]?.has(col.name)) row.classList.add('preview-modified');
    body.appendChild(row);
  }
  // Append removed columns (absent from proposed — source from original schema)
  const removedColNames = changeIndex.removedCols?.[table.name];
  if (removedColNames?.size) {
    const origTable = (schema.tables ?? []).find(t => t.name === table.name);
    for (const col of (origTable?.columns ?? [])) {
      if (removedColNames.has(col.name)) {
        const row = buildColRow(col, table.name);
        row.classList.add('preview-removed');
        body.appendChild(row);
      }
    }
  }
  card.appendChild(body);

  // ── Events
  card.addEventListener('mousedown', onDragStart);
  card.addEventListener('click', e => { e.stopPropagation(); selectTable(table.name, e.shiftKey); });
  card.addEventListener('contextmenu', e => { e.preventDefault(); e.stopPropagation(); showCtxMenu(e, table.name); });

  return card;
}

function buildColRow(col, tableName) {
  const row = document.createElement('div');
  row.className = 'col-row';
  row.dataset.col = col.name;

  // Derive FK status from col.foreign_key OR from the effective relations list.
  // Using relations as the source of truth means newly-added FKs show the badge
  // even before the schema file is updated with foreign_key field values.
  const isFk = !col.primary_key && (
    col.foreign_key ||
    (tableName && effectiveRelations().some(r => r.from_table === tableName && r.from_column === col.name))
  );

  const badge = document.createElement('span');
  if      (col.primary_key) { badge.className = 'col-badge badge-pk'; badge.textContent = 'PK'; }
  else if (isFk)            { badge.className = 'col-badge badge-fk'; badge.textContent = 'FK'; }
  else if (col.unique)      { badge.className = 'col-badge badge-uq'; badge.textContent = 'UQ'; }
  else                      { badge.className = 'col-badge badge-empty'; badge.textContent = ''; }

  const name = document.createElement('span');
  name.className   = 'col-name' + (col.primary_key ? ' is-pk' : isFk ? ' is-fk' : '');
  name.textContent = col.name;

  const nullable = document.createElement('span');
  nullable.className   = 'col-null';
  nullable.textContent = col.nullable !== false ? '?' : '';

  const isEnumType = schema ? effectiveEnums().some(e => e.name === col.type) : false;
  const type = document.createElement('span');
  type.className   = 'col-type' + (isEnumType ? ' is-enum' : '');
  type.textContent = typeLabel(col);

  row.appendChild(badge); row.appendChild(name); row.appendChild(nullable); row.appendChild(type);

  if (tableName) {
    row.addEventListener('contextmenu', e => {
      e.preventDefault();
      e.stopPropagation();
      showCtxMenu(e, tableName, col.name);
    });
  }

  return row;
}

// ─── Drag ─────────────────────────────────────────────────────────────────────

function onDragStart(e) {
  if (e.button !== 0) return;
  e.preventDefault();
  e.stopPropagation();
  const card = e.currentTarget;
  card.classList.add('dragging');
  dragging = {
    card,
    tableName:   card.dataset.table,
    startMouseX: e.clientX,
    startMouseY: e.clientY,
    startCardX:  parseInt(card.style.left, 10) || 0,
    startCardY:  parseInt(card.style.top,  10) || 0,
  };
}

document.addEventListener('mousemove', e => {
  if (!dragging) return;
  const scale = pz ? pz.getTransform().scale : 1;
  const x = Math.max(0, dragging.startCardX + (e.clientX - dragging.startMouseX) / scale);
  const y = Math.max(0, dragging.startCardY + (e.clientY - dragging.startMouseY) / scale);
  dragging.card.style.left = x + 'px';
  dragging.card.style.top  = y + 'px';
  renderRelations();
  updateMinimap();
});

document.addEventListener('mouseup', async e => {
  if (!dragging) return;
  const { card, tableName } = dragging;
  card.classList.remove('dragging');
  const x = parseInt(card.style.left, 10);
  const y = parseInt(card.style.top,  10);
  dragging = null;

  try {
    await fetch('/api/position', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ table: tableName, x, y }),
    });
  } catch (_) {}

  const allTables = [
    ...(schema.tables ?? []),
    ...(schema.proposed_schema?.tables ?? []),
  ];
  const t = allTables.find(t => t.name === tableName);
  if (t) t.position = { x, y };

  updateHighlights();
  updateMinimap();
});

// Click on empty canvas → deselect + close context menu
document.getElementById('canvas-wrap').addEventListener('click', e => {
  if (!e.target.closest('.table-card')) {
    selectedTables.clear();
    updateHighlights();
    updateMinimap();
  }
  hideCtxMenu();
});

// ─── Selection ────────────────────────────────────────────────────────────────

function selectTable(name, addToSelection = false) {
  if (addToSelection) {
    if (selectedTables.has(name)) selectedTables.delete(name);
    else selectedTables.add(name);
  } else {
    if (selectedTables.size === 1 && selectedTables.has(name)) selectedTables.clear();
    else { selectedTables.clear(); selectedTables.add(name); }
  }
  updateHighlights();
  updateMinimap();
}

function updateHighlights() {
  const relations = effectiveRelations();
  const related = new Set();
  for (const sel of selectedTables) {
    for (const rel of (relations ?? [])) {
      if (rel.from_table === sel) related.add(rel.to_table);
      if (rel.to_table   === sel) related.add(rel.from_table);
    }
  }
  for (const [name, el] of Object.entries(tableEls)) {
    el.classList.toggle('selected', selectedTables.has(name));
    el.classList.toggle('related',  related.has(name) && !selectedTables.has(name));
  }
  const svg = document.getElementById('canvas-svg');
  for (const path of svg.querySelectorAll('.rel-path')) {
    const from = path.dataset.from;
    const to   = path.dataset.to;
    const active = selectedTables.size > 0 && (selectedTables.has(from) || selectedTables.has(to));
    path.classList.toggle('highlighted', active);
    path.classList.toggle('dimmed', selectedTables.size > 0 && !active);
  }
}

// ─── Relation rendering ───────────────────────────────────────────────────────

function renderRelations() {
  const svg = document.getElementById('canvas-svg');
  for (const el of svg.querySelectorAll('.rel-path, .rel-hit')) el.remove();

  const relations = effectiveRelations();
  const addedRelTables = new Set(
    (schema.changes ?? []).filter(c => c.type === 'add_relation').map(c => c.table)
  );

  for (const rel of (relations ?? [])) {
    const fromEl = tableEls[rel.from_table];
    const toEl   = tableEls[rel.to_table];
    if (!fromEl || !toEl || fromEl === toEl) continue;
    const previewState = addedRelTables.has(rel.from_table) ? 'new' : null;
    const { path, hitPath } = buildRelPath(rel, fromEl, toEl, previewState);
    if (path) { svg.appendChild(path); svg.appendChild(hitPath); }
  }

  updateHighlights();
}

function buildRelPath(rel, fromEl, toEl, previewState = null) {
  const fX = parseInt(fromEl.style.left, 10), fY = parseInt(fromEl.style.top,  10);
  const fW = fromEl.offsetWidth  || CARD_W,   fH = fromEl.offsetHeight || 120;
  const tX = parseInt(toEl.style.left, 10),   tY = parseInt(toEl.style.top,  10);
  const tW = toEl.offsetWidth    || CARD_W,   tH = toEl.offsetHeight   || 120;

  const fCx = fX + fW / 2, fCy = fY + fH / 2;
  const tCx = tX + tW / 2, tCy = tY + tH / 2;
  const dX  = tCx - fCx,   dY  = tCy - fCy;

  const fEdges = {
    right:  { x: fX + fW, y: fCy, dx:  1, dy:  0 },
    left:   { x: fX,      y: fCy, dx: -1, dy:  0 },
    bottom: { x: fCx, y: fY + fH, dx:  0, dy:  1 },
    top:    { x: fCx, y: fY,      dx:  0, dy: -1 },
  };
  const tEdges = {
    left:   { x: tX,      y: tCy, dx: -1, dy:  0 },
    right:  { x: tX + tW, y: tCy, dx:  1, dy:  0 },
    top:    { x: tCx, y: tY,      dx:  0, dy: -1 },
    bottom: { x: tCx, y: tY + tH, dx:  0, dy:  1 },
  };

  let fromEdge, toEdge;
  const absX = Math.abs(dX), absY = Math.abs(dY);
  if      (absX >= absY * 1.1) { fromEdge = dX >= 0 ? fEdges.right  : fEdges.left;   toEdge = dX >= 0 ? tEdges.left   : tEdges.right; }
  else if (absY >  absX * 1.1) { fromEdge = dY >= 0 ? fEdges.bottom : fEdges.top;    toEdge = dY >= 0 ? tEdges.top    : tEdges.bottom; }
  else                         { fromEdge = absX > 40 ? (dX >= 0 ? fEdges.right : fEdges.left) : fEdges.right;
                                 toEdge   = absX > 40 ? (dX >= 0 ? tEdges.left  : tEdges.right) : tEdges.right; }

  const x1 = fromEdge.x, y1 = fromEdge.y, x2 = toEdge.x, y2 = toEdge.y;
  const dist = Math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2);
  const cp   = Math.max(55, dist * 0.42);
  const d = `M ${x1} ${y1} C ${x1 + fromEdge.dx * cp} ${y1 + fromEdge.dy * cp} ${x2 + toEdge.dx * cp} ${y2 + toEdge.dy * cp} ${x2} ${y2}`;

  const relType = rel.type ?? 'many-to-one';
  let mStart = 'url(#ms-many)', mEnd = 'url(#me-one)';
  if      (relType === 'one-to-many')  { mStart = 'url(#ms-one)';  mEnd = 'url(#me-many)'; }
  else if (relType === 'one-to-one')   { mStart = 'url(#ms-one)';  mEnd = 'url(#me-one)'; }
  else if (relType === 'many-to-many') { mStart = 'url(#ms-many)'; mEnd = 'url(#me-many)'; }

  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('class', 'rel-path');
  path.setAttribute('d', d);
  path.setAttribute('marker-start', mStart);
  path.setAttribute('marker-end',   mEnd);
  path.dataset.from = rel.from_table;
  path.dataset.to   = rel.to_table;
  if (previewState === 'new')     path.classList.add('preview-new');
  if (previewState === 'removed') path.classList.add('preview-removed');

  const label   = `${rel.from_column}  →  ${rel.to_column}`;
  const hitPath = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  hitPath.setAttribute('class', 'rel-hit');
  hitPath.setAttribute('d', d);
  hitPath.setAttribute('stroke', 'transparent');
  hitPath.setAttribute('stroke-width', '10');
  hitPath.dataset.from = rel.from_table;
  hitPath.dataset.to   = rel.to_table;
  hitPath.addEventListener('mouseenter', e => showTooltip(e, label));
  hitPath.addEventListener('mousemove',  moveTooltip);
  hitPath.addEventListener('mouseleave', hideTooltip);

  return { path, hitPath };
}

// ─── Tooltip ──────────────────────────────────────────────────────────────────

function showTooltip(e, text) {
  const tt = document.getElementById('rel-tooltip');
  tt.textContent = text; tt.hidden = false; moveTooltip(e);
}
function moveTooltip(e) {
  const tt = document.getElementById('rel-tooltip');
  tt.style.left = (e.clientX + 14) + 'px'; tt.style.top = (e.clientY - 6) + 'px';
}
function hideTooltip() { document.getElementById('rel-tooltip').hidden = true; }

// ─── Minimap ──────────────────────────────────────────────────────────────────

function getMinimapTransform() {
  const tables = effectiveTables();
  if (!tables.length) return { sf: 1, ox: 0, oy: 0 };
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const t of tables) {
    const x = t.position?.x ?? 0, y = t.position?.y ?? 0;
    minX = Math.min(minX, x); minY = Math.min(minY, y);
    maxX = Math.max(maxX, x + CARD_W); maxY = Math.max(maxY, y + getCardHeight(t));
  }
  const sf = Math.min((MINI_W - MINI_PAD * 2) / Math.max(maxX - minX, 1), (MINI_H - MINI_PAD * 2) / Math.max(maxY - minY, 1));
  return { sf, ox: (MINI_W - (maxX - minX) * sf) / 2 - minX * sf, oy: (MINI_H - (maxY - minY) * sf) / 2 - minY * sf };
}

function updateMinimap() {
  const mc = document.getElementById('minimap-canvas');
  if (!mc) return;
  const ctx = mc.getContext('2d');
  ctx.clearRect(0, 0, MINI_W, MINI_H);
  const tables = effectiveTables();
  if (!tables.length) return;
  const { sf, ox, oy } = getMinimapTransform();
  for (const t of tables) {
    const x = (t.position?.x ?? 0) * sf + ox, y = (t.position?.y ?? 0) * sf + oy;
    const w = Math.max(CARD_W * sf, 2), h = Math.max(getCardHeight(t) * sf, 2);
    ctx.fillStyle = selectedTables.has(t.name) ? '#3a60d0' : '#1f2d5e';
    ctx.fillRect(x, y, w, Math.min(HEADER_H * sf, h));
    ctx.fillStyle = selectedTables.has(t.name) ? '#2a40a0' : '#1a1d2e';
    const bodyTop = y + Math.min(HEADER_H * sf, h);
    if (bodyTop < y + h) ctx.fillRect(x, bodyTop, w, y + h - bodyTop);
  }
  if (pz) {
    const pzt = pz.getTransform();
    const wrap = document.getElementById('canvas-wrap');
    ctx.strokeStyle = 'rgba(91,141,238,.65)';
    ctx.lineWidth   = 1;
    ctx.strokeRect(
      (-pzt.x / pzt.scale) * sf + ox,
      (-pzt.y / pzt.scale) * sf + oy,
      (wrap.clientWidth  / pzt.scale) * sf,
      (wrap.clientHeight / pzt.scale) * sf,
    );
  }
}

function setupMinimap() {
  document.getElementById('minimap-canvas').addEventListener('click', e => {
    if (!pz) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const { sf, ox, oy } = getMinimapTransform();
    const canvasX = (e.clientX - rect.left  - ox) / sf;
    const canvasY = (e.clientY - rect.top   - oy) / sf;
    const wrap    = document.getElementById('canvas-wrap');
    const scale   = pz.getTransform().scale;
    pz.moveTo(wrap.clientWidth / 2 - canvasX * scale, wrap.clientHeight / 2 - canvasY * scale);
    updateMinimap();
  });
}

// ─── Sidebar ──────────────────────────────────────────────────────────────────

let _activeTab = 'schema'; // 'schema' | 'migration'

function setupSidebar() {
  document.getElementById('btn-sidebar').addEventListener('click', () => {
    const sidebar = document.getElementById('sidebar');
    const isOpen = sidebar.classList.contains('sidebar-open');
    sidebar.classList.toggle('sidebar-open',  !isOpen);
    sidebar.classList.toggle('sidebar-closed', isOpen);
    if (!isOpen) refreshSidebar();
  });
  document.getElementById('btn-sidebar-close').addEventListener('click', () => {
    document.getElementById('sidebar').classList.replace('sidebar-open', 'sidebar-closed');
  });

  // Tab switching
  document.getElementById('tab-schema').addEventListener('click', () => switchTab('schema'));
  document.getElementById('tab-migration').addEventListener('click', () => switchTab('migration'));

  // Copy buttons
  makeCopyBtn('btn-copy-schema-sql', 'sidebar-schema-sql');
  makeCopyBtn('btn-copy-sql',        'sidebar-sql');
}

function makeCopyBtn(btnId, preId) {
  document.getElementById(btnId).addEventListener('click', () => {
    const sql = document.getElementById(preId).textContent;
    navigator.clipboard.writeText(sql).catch(() => {});
    const btn = document.getElementById(btnId);
    const prev = btn.textContent;
    btn.textContent = 'Copied!';
    setTimeout(() => { btn.textContent = prev; }, 1500);
  });
}

function switchTab(tab) {
  _activeTab = tab;
  document.getElementById('tab-schema').classList.toggle('sidebar-tab-active',    tab === 'schema');
  document.getElementById('tab-migration').classList.toggle('sidebar-tab-active', tab === 'migration');
  document.getElementById('panel-schema').hidden    = tab !== 'schema';
  document.getElementById('panel-migration').hidden = tab !== 'migration';
  refreshSidebar();
}

async function refreshSidebar() {
  if (!document.getElementById('sidebar').classList.contains('sidebar-open')) return;
  if (_activeTab === 'schema') {
    await refreshSchemaTab();
  } else {
    await refreshMigrationTab();
  }
}

async function refreshSchemaTab() {
  try {
    const data = await fetch('/api/schema-sql').then(r => r.json());
    document.getElementById('sidebar-schema-sql').textContent = data.sql ?? '';
  } catch (_) {}
}

async function refreshMigrationTab() {
  try {
    const data = await fetch('/api/migrate').then(r => r.json());
    const sql = data.sql ?? '';
    const pre = document.getElementById('sidebar-sql');
    const empty = document.getElementById('sidebar-empty');
    const copyBtn = document.getElementById('btn-copy-sql');
    if (sql.trim()) {
      pre.textContent = sql; pre.hidden = false; empty.hidden = true; copyBtn.hidden = false;
    } else {
      pre.hidden = true; empty.hidden = false; copyBtn.hidden = true;
    }
  } catch (_) {}
}

// ─── Toast notification system ────────────────────────────────────────────────

/**
 * showToast(message, variant, duration)
 *
 * Renders a transient toast at the bottom-center of the viewport.
 * variant: 'success' | 'warning' | 'error'
 * duration: ms before auto-dismiss (default 2500)
 * Toasts stack visually (newest on top) and can be clicked to dismiss early.
 */
function showToast(message, variant = 'success', duration = 2500) {
  const stack = document.getElementById('toast-stack');
  const ICONS = { success: '✓', warning: '⚠', error: '✕' };

  const toast = document.createElement('div');
  toast.className = `toast toast-${variant}`;
  toast.setAttribute('role', 'status');
  toast.setAttribute('aria-live', 'polite');
  toast.innerHTML =
    `<span class="toast-icon" aria-hidden="true">${ICONS[variant] ?? '•'}</span>` +
    `<span class="toast-msg">${message}</span>`;

  stack.appendChild(toast);

  const dismiss = () => {
    if (!toast.parentNode) return;
    toast.classList.add('toast-exit');
    toast.addEventListener('animationend', () => toast.remove(), { once: true });
    // Fallback in case animationend never fires (hidden tab, reduced-motion, etc.)
    setTimeout(() => { if (toast.parentNode) toast.remove(); }, 350);
  };

  setTimeout(dismiss, duration);
  toast.addEventListener('click', dismiss, { once: true });
}

// ─── Code sync actions (apply-to-code / sync-from-code) ───────────────────────

function setupCodeActions() {
  const applyBtn = document.getElementById('btn-apply-code');
  const syncBtn  = document.getElementById('btn-sync-code');

  // Wrap button text in a <span class="btn-label"> so the CSS spinner
  // can hide it (visibility: hidden) without mutating textContent or
  // changing the button's intrinsic width.
  [applyBtn, syncBtn].forEach(btn => {
    const label = document.createElement('span');
    label.className = 'btn-label';
    label.textContent = btn.textContent.trim();
    btn.textContent = '';
    btn.appendChild(label);
  });

  // ── Apply to Code ──────────────────────────────────────────────────────────
  applyBtn.addEventListener('click', async () => {
    applyBtn.disabled = true;
    applyBtn.classList.add('is-loading');
    try {
      const res = await fetch('/api/apply-to-code', { method: 'POST' });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ error: 'Unknown error' }));
        showToast('Apply error: ' + err.error, 'error', 5000);
        return;
      }
      const data = await res.json();
      if (data.message?.toLowerCase().includes('up to date')) {
        showToast('Already up to date', 'warning');
      } else {
        showToast('Applied to code', 'success');
      }
    } catch (e) {
      showToast('Network error: ' + e.message, 'error', 5000);
    } finally {
      applyBtn.disabled = false;
      applyBtn.classList.remove('is-loading');
    }
  });

  // ── Sync from Code ─────────────────────────────────────────────────────────
  syncBtn.addEventListener('click', async () => {
    syncBtn.disabled = true;
    syncBtn.classList.add('is-loading');
    try {
      const res = await fetch('/api/sync-from-code', { method: 'POST' });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ error: 'Unknown error' }));
        showToast('Sync error: ' + err.error, 'error', 5000);
        return;
      }
      // Response body IS the full schema — use it directly to avoid a
      // second GET round-trip (server also SSE-broadcasts the update).
      schema = await res.json();
      updateToolbarMeta();
      updatePendingUI();
      renderAll();
      showToast('Synced from code', 'success');
    } catch (e) {
      showToast('Network error: ' + e.message, 'error', 5000);
    } finally {
      syncBtn.disabled = false;
      syncBtn.classList.remove('is-loading');
    }
  });
}

// ─── Pending actions (commit / discard) ───────────────────────────────────────

function setupPendingActions() {
  document.getElementById('btn-commit').addEventListener('click', async () => {
    await fetch('/api/commit', { method: 'POST' });
    await refreshSchema();
  });
  document.getElementById('btn-discard').addEventListener('click', async () => {
    await fetch('/api/discard', { method: 'POST' });
    await refreshSchema();
  });
}

// ─── Add Table modal ──────────────────────────────────────────────────────────

function setupAddTable() {
  document.getElementById('btn-add-table').addEventListener('click', () => {
    document.getElementById('input-table-name').value = '';
    document.getElementById('modal-add-table').hidden = false;
    setTimeout(() => document.getElementById('input-table-name').focus(), 30);
  });
  document.getElementById('btn-add-table-cancel').addEventListener('click', () => {
    document.getElementById('modal-add-table').hidden = true;
  });
  document.getElementById('btn-add-table-ok').addEventListener('click', addTableSubmit);
  document.getElementById('input-table-name').addEventListener('keydown', e => {
    if (e.key === 'Enter')  addTableSubmit();
    if (e.key === 'Escape') document.getElementById('modal-add-table').hidden = true;
  });
  document.getElementById('modal-add-table').addEventListener('click', e => {
    if (e.target === e.currentTarget) e.currentTarget.hidden = true;
  });
}

async function addTableSubmit() {
  const name = document.getElementById('input-table-name').value.trim();
  if (!name) return;
  document.getElementById('modal-add-table').hidden = true;
  const pos = viewportCentreInCanvas();
  await fetch('/api/propose', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ op: 'add_table', name, x: pos.x, y: pos.y }),
  });
  await refreshSchema();
}

function viewportCentreInCanvas() {
  if (!pz) return { x: 400, y: 300 };
  const t = pz.getTransform();
  const wrap = document.getElementById('canvas-wrap');
  return { x: Math.round((wrap.clientWidth / 2 - t.x) / t.scale), y: Math.round((wrap.clientHeight / 2 - t.y) / t.scale) };
}

// ─── Add Column modal ─────────────────────────────────────────────────────────

let _addColTable = null;

function openAddColModal(tableName) {
  _addColTable = tableName;
  document.getElementById('modal-add-col-table').textContent = tableName;
  document.getElementById('input-col-name').value = '';
  populateTypeSelect(document.getElementById('input-col-type'), 'string');
  document.getElementById('input-col-nullable').checked  = true;
  document.getElementById('input-col-unique').checked    = false;
  document.getElementById('input-col-pk').checked        = false;

  // Show PK checkbox only when the table has no primary key yet.
  const tbl   = effectiveTables().find(t => t.name === tableName);
  const hasPk = tbl?.columns?.some(c => c.primary_key) ?? false;
  document.getElementById('label-col-pk').hidden         = hasPk;
  document.getElementById('label-col-nullable').hidden   = false;
  document.getElementById('input-col-nullable').disabled = false;

  document.getElementById('modal-add-col').hidden = false;
  setTimeout(() => document.getElementById('input-col-name').focus(), 30);
}

function setupAddCol() {
  document.getElementById('btn-add-col-cancel').addEventListener('click', () => {
    document.getElementById('modal-add-col').hidden = true;
  });
  document.getElementById('btn-add-col-ok').addEventListener('click', addColSubmit);
  document.getElementById('input-col-name').addEventListener('keydown', e => {
    if (e.key === 'Enter')  addColSubmit();
    if (e.key === 'Escape') document.getElementById('modal-add-col').hidden = true;
  });
  document.getElementById('modal-add-col').addEventListener('click', e => {
    if (e.target === e.currentTarget) e.currentTarget.hidden = true;
  });
  // When PK is toggled, force nullable=false and disable the nullable checkbox.
  document.getElementById('input-col-pk').addEventListener('change', e => {
    const isPk = e.target.checked;
    document.getElementById('input-col-nullable').checked  = !isPk;
    document.getElementById('input-col-nullable').disabled = isPk;
    if (isPk) document.getElementById('input-col-unique').checked = false;
  });
}

async function addColSubmit() {
  if (!_addColTable) return;
  const name       = document.getElementById('input-col-name').value.trim();
  const type       = document.getElementById('input-col-type').value;
  const primaryKey = !document.getElementById('label-col-pk').hidden &&
                     document.getElementById('input-col-pk').checked;
  const nullable   = primaryKey ? false : document.getElementById('input-col-nullable').checked;
  const unique     = document.getElementById('input-col-unique').checked;
  if (!name) return;
  document.getElementById('modal-add-col').hidden = true;
  const colData = { name, type, nullable, unique, primary_key: primaryKey, foreign_key: '' };
  // Seed uuid4 default for uuid PK columns — matches the convention used by add_table.
  if (primaryKey && type === 'uuid') colData.default = 'uuid4';
  await fetch('/api/propose', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ op: 'add_column', table: _addColTable, column: colData }),
  });
  await refreshSchema();
}

// ─── Paste SQL modal ──────────────────────────────────────────────────────────

function setupPasteSQL() {
  document.getElementById('btn-paste-sql').addEventListener('click', () => {
    document.getElementById('input-paste-sql').value = '';
    document.getElementById('modal-paste-sql').hidden = false;
    setTimeout(() => document.getElementById('input-paste-sql').focus(), 30);
  });
  document.getElementById('btn-paste-sql-cancel').addEventListener('click', () => {
    document.getElementById('modal-paste-sql').hidden = true;
  });
  document.getElementById('btn-paste-sql-ok').addEventListener('click', pasteSQLSubmit);
  document.getElementById('modal-paste-sql').addEventListener('click', e => {
    if (e.target === e.currentTarget) e.currentTarget.hidden = true;
  });
}

async function pasteSQLSubmit() {
  const sql = document.getElementById('input-paste-sql').value.trim();
  if (!sql) return;
  document.getElementById('modal-paste-sql').hidden = true;
  const res = await fetch('/api/paste-sql', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sql }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: 'Unknown error' }));
    alert(`Parse error: ${err.error}`);
    return;
  }
  await refreshSchema();
}

// ─── Template picker ──────────────────────────────────────────────────────────

function setupTemplates() {
  document.getElementById('btn-template').addEventListener('click', async () => {
    const data = await fetch('/api/templates').then(r => r.json());
    const list = document.getElementById('template-list');
    list.innerHTML = '';
    const LABELS = {
      'saas-base': 'SaaS Base — users, orgs, billing, audit',
      'auth':      'Auth — users, sessions, tokens',
      'cms':       'CMS — posts, pages, media, tags',
      'ecommerce': 'E-commerce — products, orders, cart',
    };
    for (const name of (data.templates ?? [])) {
      const btn = document.createElement('button');
      btn.className   = 'template-item';
      btn.textContent = LABELS[name] ?? name;
      btn.addEventListener('click', () => loadTemplate(name));
      list.appendChild(btn);
    }
    document.getElementById('modal-template').hidden = false;
  });
  document.getElementById('btn-template-cancel').addEventListener('click', () => {
    document.getElementById('modal-template').hidden = true;
  });
  document.getElementById('modal-template').addEventListener('click', e => {
    if (e.target === e.currentTarget) e.currentTarget.hidden = true;
  });
}

async function loadTemplate(name) {
  document.getElementById('modal-template').hidden = true;
  await fetch('/api/template', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  });
  await refreshSchema();
  requestAnimationFrame(fitToScreen);
}

// ─── Edit Column modal ────────────────────────────────────────────────────────

let _editColTable = null;
let _editColName  = null;

function openEditColModal(tableName, colName) {
  const tables = effectiveTables();
  const tbl = tables.find(t => t.name === tableName);
  const col = tbl?.columns?.find(c => c.name === colName);
  if (!col) return;

  _editColTable = tableName;
  _editColName  = colName;

  document.getElementById('modal-edit-col-label').textContent = `${tableName}.${colName}`;
  document.getElementById('input-edit-col-name').value     = col.name;
  populateTypeSelect(document.getElementById('input-edit-col-type'), col.type);
  document.getElementById('input-edit-col-nullable').checked = !!col.nullable;
  document.getElementById('input-edit-col-unique').checked   = !!col.unique;
  document.getElementById('modal-edit-col').hidden = false;
  setTimeout(() => document.getElementById('input-edit-col-name').focus(), 30);
}

function setupEditCol() {
  document.getElementById('btn-edit-col-cancel').addEventListener('click', () => {
    document.getElementById('modal-edit-col').hidden = true;
  });
  document.getElementById('btn-edit-col-ok').addEventListener('click', editColSubmit);
  document.getElementById('input-edit-col-name').addEventListener('keydown', e => {
    if (e.key === 'Enter')  editColSubmit();
    if (e.key === 'Escape') document.getElementById('modal-edit-col').hidden = true;
  });
  document.getElementById('modal-edit-col').addEventListener('click', e => {
    if (e.target === e.currentTarget) e.currentTarget.hidden = true;
  });
}

async function editColSubmit() {
  if (!_editColTable || !_editColName) return;
  const name     = document.getElementById('input-edit-col-name').value.trim();
  const type     = document.getElementById('input-edit-col-type').value;
  const nullable = document.getElementById('input-edit-col-nullable').checked;
  const unique   = document.getElementById('input-edit-col-unique').checked;
  if (!name) return;
  document.getElementById('modal-edit-col').hidden = true;
  await fetch('/api/propose', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      op: 'modify_column', table: _editColTable, column: _editColName,
      updates: { name, type, nullable, unique },
    }),
  });
  await refreshSchema();
}

// ─── Add Foreign Key modal ────────────────────────────────────────────────────

let _fkTable = null;

function openAddFkModal(tableName) {
  _fkTable = tableName;
  document.getElementById('modal-fk-table').textContent = tableName;

  const tables = effectiveTables();
  const tbl    = tables.find(t => t.name === tableName);

  // Populate source columns (non-PK columns of this table)
  const colSel = document.getElementById('input-fk-col');
  colSel.innerHTML = '';
  for (const col of (tbl?.columns ?? [])) {
    if (col.primary_key) continue;
    const opt = document.createElement('option');
    opt.value = col.name; opt.textContent = col.name;
    colSel.appendChild(opt);
  }

  // Populate target tables (all other tables)
  const toTableSel = document.getElementById('input-fk-to-table');
  toTableSel.innerHTML = '';
  for (const t of tables) {
    if (t.name === tableName) continue;
    const opt = document.createElement('option');
    opt.value = t.name; opt.textContent = t.name;
    toTableSel.appendChild(opt);
  }

  populateFkTargetCols();
  document.getElementById('modal-add-fk').hidden = false;
}

function populateFkTargetCols() {
  const toTable = document.getElementById('input-fk-to-table').value;
  const tables  = effectiveTables();
  const tbl     = tables.find(t => t.name === toTable);
  const toColSel = document.getElementById('input-fk-to-col');
  toColSel.innerHTML = '';
  for (const col of (tbl?.columns ?? [])) {
    const opt = document.createElement('option');
    opt.value = col.name;
    opt.textContent = col.name + (col.primary_key ? ' (PK)' : '');
    if (col.primary_key) opt.selected = true;
    toColSel.appendChild(opt);
  }
}

function setupAddFk() {
  document.getElementById('input-fk-to-table').addEventListener('change', populateFkTargetCols);
  document.getElementById('btn-add-fk-cancel').addEventListener('click', () => {
    document.getElementById('modal-add-fk').hidden = true;
  });
  document.getElementById('btn-add-fk-ok').addEventListener('click', addFkSubmit);
  document.getElementById('modal-add-fk').addEventListener('click', e => {
    if (e.target === e.currentTarget) e.currentTarget.hidden = true;
  });
}

async function addFkSubmit() {
  if (!_fkTable) return;
  const fromCol  = document.getElementById('input-fk-col').value;
  const toTable  = document.getElementById('input-fk-to-table').value;
  const toCol    = document.getElementById('input-fk-to-col').value;
  if (!fromCol || !toTable || !toCol) return;
  document.getElementById('modal-add-fk').hidden = true;
  await fetch('/api/propose', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      op: 'add_relation',
      relation: {
        name: `${_fkTable}_${fromCol}_${toTable}_fkey`,
        from_table: _fkTable, from_column: fromCol,
        to_table: toTable,    to_column: toCol,
        type: 'many-to-one',  on_delete: 'CASCADE',
      },
    }),
  });
  await refreshSchema();
}

// ─── Context menu ─────────────────────────────────────────────────────────────

let _ctxTable = null;
let _ctxCol   = null;

function showCtxMenu(e, tableName, colName = null) {
  _ctxTable = tableName;
  _ctxCol   = colName;

  // Toggle table-level vs column-level items
  const isCol = colName != null;
  document.getElementById('ctx-add-col').hidden    = isCol;
  document.getElementById('ctx-add-fk').hidden     = isCol;
  document.getElementById('ctx-drop-table').hidden = isCol;
  document.getElementById('ctx-col-sep').hidden    = !isCol;
  document.getElementById('ctx-edit-col').hidden   = !isCol;
  document.getElementById('ctx-drop-col').hidden   = !isCol;

  const menu = document.getElementById('ctx-menu');
  menu.hidden = false;
  menu.style.left = Math.min(e.clientX, window.innerWidth  - 180) + 'px';
  menu.style.top  = Math.min(e.clientY, window.innerHeight - 100) + 'px';
}

function hideCtxMenu() {
  document.getElementById('ctx-menu').hidden = true;
  _ctxTable = null;
  _ctxCol   = null;
}

function setupCtxMenu() {
  // Table-level
  document.getElementById('ctx-add-col').addEventListener('click', () => {
    const name = _ctxTable;
    hideCtxMenu();
    if (name) openAddColModal(name);
  });
  document.getElementById('ctx-add-fk').addEventListener('click', () => {
    const name = _ctxTable;
    hideCtxMenu();
    if (name) openAddFkModal(name);
  });
  document.getElementById('ctx-drop-table').addEventListener('click', async () => {
    const name = _ctxTable;
    hideCtxMenu();
    if (!name) return;
    await fetch('/api/propose', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ op: 'drop_table', name }),
    });
    selectedTables.delete(name);
    await refreshSchema();
  });

  // Column-level
  document.getElementById('ctx-edit-col').addEventListener('click', () => {
    const tbl = _ctxTable, col = _ctxCol;
    hideCtxMenu();
    if (tbl && col) openEditColModal(tbl, col);
  });
  document.getElementById('ctx-drop-col').addEventListener('click', async () => {
    const tbl = _ctxTable, col = _ctxCol;
    hideCtxMenu();
    if (!tbl || !col) return;
    await fetch('/api/propose', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ op: 'drop_column', table: tbl, column: col }),
    });
    await refreshSchema();
  });

  document.addEventListener('click', e => { if (!e.target.closest('#ctx-menu')) hideCtxMenu(); });
}

// ─── Keyboard shortcuts ───────────────────────────────────────────────────────

document.addEventListener('keydown', e => {
  const inInput = e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT';

  if (e.key === 'Escape') {
    if (!inInput) { selectedTables.clear(); updateHighlights(); updateMinimap(); }
    hideCtxMenu();
    return;
  }
  if (inInput) return;

  if ((e.ctrlKey || e.metaKey) && e.key === 'z' && !e.shiftKey) {
    e.preventDefault();
    fetch('/api/undo', { method: 'POST' }).then(() => refreshSchema());
    return;
  }
  if ((e.ctrlKey || e.metaKey) && (e.shiftKey && e.key === 'z' || e.key === 'y')) {
    e.preventDefault();
    fetch('/api/redo', { method: 'POST' }).then(() => refreshSchema());
    return;
  }
  if ((e.ctrlKey || e.metaKey) && e.key === 'a') {
    e.preventDefault();
    for (const t of effectiveTables()) selectedTables.add(t.name);
    updateHighlights(); updateMinimap();
    return;
  }
  if ((e.key === 'Delete' || e.key === 'Backspace') && selectedTables.size > 0) {
    e.preventDefault();
    const names = [...selectedTables];
    selectedTables.clear();
    Promise.all(names.map(name =>
      fetch('/api/propose', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ op: 'drop_table', name }),
      })
    )).then(() => refreshSchema());
    return;
  }
  if (e.key === '+' || (e.key === '=' && !e.shiftKey)) {
    e.preventDefault();
    if (pz) { const w = document.getElementById('canvas-wrap'); pz.smoothZoom(w.clientWidth / 2, w.clientHeight / 2, 1.4); }
    return;
  }
  if (e.key === '-') {
    e.preventDefault();
    if (pz) { const w = document.getElementById('canvas-wrap'); pz.smoothZoom(w.clientWidth / 2, w.clientHeight / 2, 1 / 1.4); }
    return;
  }
  if (e.key === 'f' || e.key === 'F') { e.preventDefault(); fitToScreen(); }
});

// ─── SSE live-sync ────────────────────────────────────────────────────────────

function setupSSE() {
  if (_sseSource) { _sseSource.close(); }

  _sseSource = new EventSource('/api/events');

  _sseSource.onmessage = function(e) {
    let data;
    try { data = JSON.parse(e.data); } catch { return; }

    if (data.type === 'schema_changed' || data.type === 'file_changed') {
      // Apply the full schema payload that arrived via SSE — no extra fetch needed.
      schema = data;
      updateToolbarMeta();
      updatePendingUI();
      renderAll();
      requestAnimationFrame(() => {
        renderRelations();
        updateHighlights();
        updateMinimap();
      });
      refreshSidebar();

    } else if (data.type === 'position_updated' && !dragging) {
      // Another client dragged a table — update our local state and DOM.
      const tName = data.table_name;
      for (const src of [schema.proposed_schema, schema]) {
        const tbl = (src?.tables ?? []).find(t => t.name === tName);
        if (tbl) { tbl.position = { x: data.x, y: data.y }; }
      }
      const el = tableEls[tName];
      if (el) { el.style.left = data.x + 'px'; el.style.top = data.y + 'px'; }
      updateMinimap();
    }
  };

  _sseSource.onerror = function() {
    // Browser will auto-reconnect — nothing to do here.
  };
}

// ─── Smart awareness ──────────────────────────────────────────────────────────

async function checkAwareness() {
  try {
    const data = await fetch('/api/awareness').then(r => r.json());
    const untracked = data.untracked ?? [];
    const unmapped  = data.unmapped  ?? [];

    if (untracked.length === 0 && unmapped.length === 0) return;

    const banner = document.getElementById('awareness-banner');
    const msg    = document.getElementById('awareness-msg');
    const parts  = [];

    if (untracked.length > 0) {
      parts.push(
        `${untracked.length} table${untracked.length !== 1 ? 's' : ''} found in model files but not in the diagram`
        + ` (${untracked.slice(0, 3).join(', ')}${untracked.length > 3 ? '…' : ''})`
      );
    }
    if (unmapped.length > 0) {
      parts.push(
        `${unmapped.length} table${unmapped.length !== 1 ? 's' : ''} have no matching model file`
        + ` (${unmapped.slice(0, 3).join(', ')}${unmapped.length > 3 ? '…' : ''})`
      );
    }

    msg.textContent = parts.join(' · ');
    banner.hidden = false;
  } catch {
    // awareness is best-effort — ignore network/parse errors
  }
}

function dismissAwareness() {
  document.getElementById('awareness-banner').hidden = true;
}

// ─── Enum management ──────────────────────────────────────────────────────────

let _enumEditName = null; // null = creating new, string = editing existing

function openEnumListModal() {
  renderEnumList();
  document.getElementById('modal-enum-list').hidden = false;
}

/**
 * Extract a display string from an enum value.
 * Handles both plain strings ("admin") and EnumMember objects
 * ({"member_name": "ADMIN", "value": "admin"}).
 *
 * If member_name and value differ (case-insensitively), shows "MEMBER_NAME = value"
 * so the user can see and edit both parts.  Otherwise shows just the value.
 */
function enumValueDisplay(v) {
  if (typeof v === 'string') return v;
  if (v && typeof v === 'object' && v.member_name !== undefined) {
    if (v.member_name.toLowerCase() === v.value.toLowerCase()) return v.value;
    return `${v.member_name} = ${v.value}`;
  }
  return String(v);
}

/**
 * Parse one textarea line back into an EnumMember object.
 * "ADMIN = admin"  → { member_name: "ADMIN", value: "admin" }
 * "admin"          → { member_name: "admin", value: "admin" }
 */
function parseEnumValue(line) {
  const trimmed = line.trim();
  const eqIdx = trimmed.indexOf(' = ');
  if (eqIdx !== -1) {
    return {
      member_name: trimmed.substring(0, eqIdx).trim(),
      value: trimmed.substring(eqIdx + 3).trim(),
    };
  }
  return { member_name: trimmed, value: trimmed };
}

function renderEnumList() {
  const enums = effectiveEnums();
  const container = document.getElementById('enum-list');
  container.innerHTML = '';
  if (enums.length === 0) {
    const empty = document.createElement('p');
    empty.className = 'enum-list-empty';
    empty.textContent = 'No enums defined yet.';
    container.appendChild(empty);
    return;
  }
  for (const e of enums) {
    const row = document.createElement('div');
    row.className = 'enum-row';
    const nameSpan = document.createElement('span');
    nameSpan.className = 'enum-row-name';
    nameSpan.textContent = e.name;
    const valSpan = document.createElement('span');
    valSpan.className = 'enum-row-vals';
    valSpan.textContent = e.values.map(enumValueDisplay).join(', ');
    const editBtn = document.createElement('button');
    editBtn.className = 'tb-btn enum-row-edit';
    editBtn.textContent = 'Edit';
    editBtn.addEventListener('click', () => openEnumEditModal(e));
    row.appendChild(nameSpan);
    row.appendChild(valSpan);
    row.appendChild(editBtn);
    container.appendChild(row);
  }
}

function openEnumEditModal(existingEnum) {
  _enumEditName = existingEnum ? existingEnum.name : null;
  document.getElementById('modal-enum-edit-title').textContent =
    existingEnum ? `Edit Enum — ${existingEnum.name}` : 'New Enum';
  document.getElementById('input-enum-name').value =
    existingEnum ? existingEnum.name : '';
  document.getElementById('input-enum-values').value =
    existingEnum ? existingEnum.values.map(enumValueDisplay).join('\n') : '';
  document.getElementById('btn-enum-edit-delete').hidden = !existingEnum;
  document.getElementById('modal-enum-edit').hidden = false;
  setTimeout(() => document.getElementById('input-enum-name').focus(), 30);
}

async function enumEditSubmit() {
  const name   = document.getElementById('input-enum-name').value.trim();
  const values = document.getElementById('input-enum-values').value
    .split('\n').map(v => v.trim()).filter(Boolean).map(parseEnumValue);
  if (!name || values.length === 0) return;

  document.getElementById('modal-enum-edit').hidden = true;

  if (_enumEditName === null) {
    // Create
    await fetch('/api/propose', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ op: 'add_enum', name, values }),
    });
  } else {
    // Update
    const updates = {};
    if (name !== _enumEditName) updates.name = name;
    updates.values = values;
    await fetch('/api/propose', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ op: 'edit_enum', name: _enumEditName, updates }),
    });
  }

  await refreshSchema();
  // Re-render the list if it's still open
  if (!document.getElementById('modal-enum-list').hidden) renderEnumList();
}

async function enumDeleteSubmit() {
  if (!_enumEditName) return;
  const name = _enumEditName;
  document.getElementById('modal-enum-edit').hidden = true;
  await fetch('/api/propose', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ op: 'drop_enum', name }),
  });
  await refreshSchema();
  if (!document.getElementById('modal-enum-list').hidden) renderEnumList();
}

function setupEnums() {
  document.getElementById('btn-enums').addEventListener('click', openEnumListModal);

  document.getElementById('btn-enum-list-close').addEventListener('click', () => {
    document.getElementById('modal-enum-list').hidden = true;
  });
  document.getElementById('btn-enum-new').addEventListener('click', () => {
    openEnumEditModal(null);
  });
  document.getElementById('modal-enum-list').addEventListener('click', e => {
    if (e.target === e.currentTarget) e.currentTarget.hidden = true;
  });

  document.getElementById('btn-enum-edit-cancel').addEventListener('click', () => {
    document.getElementById('modal-enum-edit').hidden = true;
  });
  document.getElementById('btn-enum-edit-ok').addEventListener('click', enumEditSubmit);
  document.getElementById('btn-enum-edit-delete').addEventListener('click', enumDeleteSubmit);
  document.getElementById('modal-enum-edit').addEventListener('click', e => {
    if (e.target === e.currentTarget) e.currentTarget.hidden = true;
  });
  document.getElementById('input-enum-name').addEventListener('keydown', e => {
    if (e.key === 'Escape') document.getElementById('modal-enum-edit').hidden = true;
  });
}

// ─── Bootstrap ────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  setupMinimap();
  setupSidebar();
  setupCodeActions();
  setupPendingActions();
  setupAddTable();
  setupAddCol();
  setupEditCol();
  setupAddFk();
  setupPasteSQL();
  setupTemplates();
  setupCtxMenu();
  setupEnums();
  init();
});
