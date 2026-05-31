// Carte interactive (Étape A): poster canon en fond + zoom/pan,
// marqueurs aux coordonnées canon, clic POI -> panneau, scénario -> highlight, recherche.
(function () {
  var dataEl = document.getElementById('carte-data');
  var svg = document.getElementById('carte-svg');
  if (!dataEl || !svg) return;
  var M = JSON.parse(dataEl.textContent);
  if (M.title) svg.setAttribute('aria-label', 'Carte schématique ' + (/^[aeiouhàâéèêAEIOUH]/.test(M.title) ? "d'" : 'de ') + M.title);
  var SVGNS = 'http://www.w3.org/2000/svg';
  var poiById = {}, poiNodes = {};
  M.pois.forEach(function (p) { poiById[p.id] = p; });
  var routeMode = false, route = [];   // pathfinding (#6): trajet + quartiers traversés

  function el(name, attrs) {
    var n = document.createElementNS(SVGNS, name);
    for (var k in attrs) { if (attrs[k] != null) n.setAttribute(k, attrs[k]); }
    return n;
  }
  function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; }
  function norm(s) { return (s || '').toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, ''); }

  // --- coordinate space = canon PDF points ---
  var VB = (M.viewBox || '0 0 1247 794').split(/\s+/).map(Number);
  var W0 = VB[2], H0 = VB[3];
  var view = { x: 0, y: 0, w: W0, h: H0 };   // current viewBox (pan/zoom)
  var lastR = null;
  function applyView() {
    svg.setAttribute('viewBox', view.x + ' ' + view.y + ' ' + view.w + ' ' + view.h);
    var r = view.w / W0;                       // <1 when zoomed in
    if (r === lastR) return;                   // pan: viewBox moved but scale unchanged → skip resize/declutter
    lastR = r;
    svg.style.setProperty('--mk', r);          // marker scale ratio
    updateSizes(r);                            // (calls declutter) — only on real zoom
  }

  // layers (drawn in canon coordinates; the viewBox does the zoom/pan)
  // order = paint/hit order: poster < zones < district labels < pois < legend hotspots
  var gPoster = el('g'), gZones = el('g'), gRivers = el('g'), gRoute = el('g'), gDist = el('g'),
      gPois = el('g'), gLegendZones = el('g'), gLegend = el('g');
  svg.appendChild(gPoster); svg.appendChild(gZones); svg.appendChild(gRivers); svg.appendChild(gRoute);
  svg.appendChild(gDist); svg.appendChild(gPois); svg.appendChild(gLegendZones); svg.appendChild(gLegend);
  // river band drawn OVER the zones → the colored sections are visibly split by the water
  (M.rivers || []).forEach(function (poly) {
    var pts = poly.map(function (p) { return p[0] + ',' + p[1]; }).join(' ');
    gRivers.appendChild(el('polygon', { points: pts, 'class': 'carte-river-band' }));
  });
  svg.setAttribute('viewBox', '0 0 ' + W0 + ' ' + H0);

  // poster underlay
  if (M.posterUrl) {
    var img = el('image', { x: 0, y: 0, width: W0, height: H0, href: M.posterUrl,
      preserveAspectRatio: 'none', opacity: (M.posterOpacity != null ? M.posterOpacity : 0.55) });
    img.setAttributeNS('http://www.w3.org/1999/xlink', 'href', M.posterUrl);
    gPoster.appendChild(img);
  }

  // --- quarters (Voronoï seeds) = district anchors + quarter-POIs ---
  var seeds = [];
  (M.districts || []).forEach(function (dz) {
    seeds.push({ x: dz.x, y: dz.y, kind: 'district', ref: dz, name: dz.name }); });
  M.pois.forEach(function (p) {
    if (p.seed) seeds.push({ x: p.x, y: p.y, kind: 'poi', ref: p, name: p.name }); });
  var seedByName = {}; seeds.forEach(function (s) { seedByName[s.name] = s; });
  // Membership is CANON (p.section / quarter tags), never distance-based.
  // The Voronoï below is used ONLY to draw the section regions.

  // district orientation labels (clickable → show quarter)
  var distLabels = [];
  (M.districts || []).forEach(function (dz) {
    var t = el('text', { x: dz.x, y: dz.y, 'class': 'carte-dist-label',
      'text-anchor': 'middle', tabindex: 0 });
    t.textContent = dz.name;
    t.addEventListener('click', function () { showQuarter(seedByName[dz.name]); });
    t.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); showQuarter(seedByName[dz.name]); } });
    gDist.appendChild(t); distLabels.push(t);
  });

  // --- zones: Voronoï cells (geometry only) coloured by CANON section ---
  // Clip box excludes the printed legend panel on the left.
  var ZX = 236;
  var SECTION_COLOR = { sud: '#9a7a2e', est: '#a8483f', nord: '#4f7a55' };
  function clipHP(poly, A, B) {  // keep the half-plane of points closer to A than B
    var dx = B.x - A.x, dy = B.y - A.y, mx = (A.x + B.x) / 2, my = (A.y + B.y) / 2;
    function f(p) { return (p[0] - mx) * dx + (p[1] - my) * dy; }
    function inter(p, q) { var a = f(p), b = f(q), t = a / (a - b);
      return [p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1])]; }
    var out = [];
    for (var i = 0; i < poly.length; i++) {
      var cur = poly[i], prv = poly[(i + poly.length - 1) % poly.length];
      var ci = f(cur) <= 0, pi = f(prv) <= 0;
      if (ci) { if (!pi) out.push(inter(prv, cur)); out.push(cur); }
      else if (pi) { out.push(inter(prv, cur)); }
    }
    return out;
  }
  // Zones are clipped to the canon city outline (inside the walls) so they don't
  // spill into the countryside. Starting the Voronoï subject as the (concave)
  // city polygon is correct because each clipHP is a convex half-plane cut.
  function qnorm(s) { return norm(s).replace(/ß/g, 'ss').replace(/[^a-z0-9]/g, ''); }
  var seedByNorm = {}; seeds.forEach(function (s) { seedByNorm[qnorm(s.name)] = s; });
  // Click a quarter by canon name → its fiche if one exists, else a minimal panel.
  function showQuarterByName(name) {
    var s = seedByNorm[qnorm(name)];
    if (s) { showQuarter(s); return; }
    var qp = (M.quarterPolygons || []).find(function (q) { return qnorm(q.name) === qnorm(name); });
    showQuarter({ kind: 'district', ref: { name: name, section: qp ? qp.section : '' } });
  }

  if (M.quarterPolygons && M.quarterPolygons.length) {
    // Real quarter contours traced from the canon boundary map (dashed-line
    // watershed, barriers = walls + water). Coloured by canon section.
    M.quarterPolygons.forEach(function (q) {
      var pts = q.poly.map(function (p) { return p[0] + ',' + p[1]; }).join(' ');
      var pg = el('polygon', { points: pts, 'class': 'carte-zone', 'data-section': q.section,
        fill: SECTION_COLOR[q.section] || '#8a7a5a' });
      var ti = el('title'); ti.textContent = q.name; pg.appendChild(ti);
      pg.addEventListener('click', function () { showQuarterByName(q.name); });
      gZones.appendChild(pg);
    });
  } else {
    // Fallback: Voronoï cells clipped to the city outline (geometry only).
    var CITY = (M.cityPolygon && M.cityPolygon.length >= 3)
      ? M.cityPolygon.map(function (p) { return [p[0], p[1]]; })
      : [[ZX, 0], [W0, 0], [W0, H0], [ZX, H0]];
    seeds.forEach(function (s, i) {
      var poly = CITY.map(function (p) { return [p[0], p[1]]; });
      for (var j = 0; j < seeds.length && poly.length >= 3; j++) {
        if (j !== i) poly = clipHP(poly, { x: s.x, y: s.y }, { x: seeds[j].x, y: seeds[j].y });
      }
      if (poly.length < 3) return;
      var pts = poly.map(function (p) { return p[0].toFixed(1) + ',' + p[1].toFixed(1); }).join(' ');
      var sec = s.ref.section;
      var pg = el('polygon', { points: pts, 'class': 'carte-zone', 'data-section': sec,
        fill: SECTION_COLOR[sec] || '#8a7a5a' });
      var ti = el('title'); ti.textContent = s.name; pg.appendChild(ti);
      pg.addEventListener('click', function () { showQuarter(s); });
      gZones.appendChild(pg);
    });
  }

  // --- colour the printed legend by section + make each section block clickable ---
  (M.legendSections || []).forEach(function (ls) {
    var r = el('rect', { x: ls.x, y: ls.y, width: ls.w, height: ls.h,
      'class': 'carte-legend-zone', 'data-section': ls.key,
      fill: SECTION_COLOR[ls.key] || '#8a7a5a' });
    var ti = el('title'); ti.textContent = ls.label; r.appendChild(ti);
    r.addEventListener('click', function () { showSection(ls.key); });
    gLegendZones.appendChild(r);
  });

  // --- clickable hotspots over the poster's printed legend (1-25) ---
  (M.legend || []).forEach(function (e) {
    if (!e.poi) return;
    var r = el('rect', { x: e.x, y: e.y, width: e.w, height: Math.min(e.h || 12, 12),
      'class': 'carte-legend-hit' });
    var ti = el('title'); ti.textContent = (poiById[e.poi] || {}).name || ''; r.appendChild(ti);
    r.addEventListener('click', function (ev) {
      ev.stopPropagation();
      var p = poiById[e.poi]; if (!p) return;
      selectPoi(e.poi); focusOn(p.x, p.y, W0 * 0.32);
    });
    gLegend.appendChild(r);
  });

  // per-type icon glyphs (centred at origin in a ±6 box, scaled with zoom)
  var TYPE_GLYPH = {
    religieux:    'M-1.3,-6 H1.3 V-2 H5 V0.6 H1.3 V6 H-1.3 V0.6 H-5 V-2 H-1.3 Z',
    magie:        'M0,-6 L1.5,-1.6 L6,-1.6 L2.4,1.2 L3.8,5.6 L0,2.9 L-3.8,5.6 L-2.4,1.2 L-6,-1.6 L-1.5,-1.6 Z',
    gouvernement: 'M-5,-4 H5 V-2 H-5 Z M-3.4,-2 H-1.7 V3 H-3.4 Z M-0.85,-2 H0.85 V3 H-0.85 Z M1.7,-2 H3.4 V3 H1.7 Z M-5,3 H5 V5 H-5 Z',
    militaire:    'M0,-6 L5,-3.5 V0.5 Q5,4.6 0,6 Q-5,4.6 -5,0.5 V-3.5 Z',
    noble:        'M-6,4 V-3 L-2.5,0.6 L0,-4.6 L2.5,0.6 L6,-3 V4 Z',
    commerce:     'M0,-6 L5.6,0 L0,6 L-5.6,0 Z',
    taverne:      'M-4.6,-4 H2 V5 H-4.6 Z M2,-2 Q5.6,-2 5.6,1 Q5.6,4 2,4 V2.2 Q3.3,2.2 3.3,1 Q3.3,-0.2 2,-0.2 Z',
    crime:        'M0,6 L-2.3,-1 H2.3 Z M-4,-1 H4 V-2.5 H-4 Z M-1,-5.6 H1 V-2.5 H-1 Z',
    mort:         'M-4,6 V-1.6 Q-4,-6 0,-6 Q4,-6 4,-1.6 V6 Z',
    autre:        'M0,-3.3 A3.3,3.3 0 1,0 0.01,-3.3 Z'
  };
  // POI markers
  var dots = [], labels = [], hits = [], glyphs = [], rings = [];
  M.pois.forEach(function (p) {
    var g = el('g', { 'class': 'carte-poi', 'data-id': p.id, 'data-type': p.type || 'autre', tabindex: 0 });
    var hit = el('circle', { cx: p.x, cy: p.y, r: 14, fill: 'transparent', 'class': 'carte-poi-hit' });
    var ring = el('circle', { cx: p.x, cy: p.y, r: 11, 'class': 'carte-poi-ring' });
    var dot = el('circle', { cx: p.x, cy: p.y, r: 6, 'class': 'carte-poi-dot' });
    var gl = el('path', { d: TYPE_GLYPH[p.type] || TYPE_GLYPH.autre, 'class': 'carte-poi-glyph' });
    var lab = el('text', { x: p.x + 9, y: p.y - 8, 'class': 'carte-poi-label' });
    lab.textContent = p.name;
    g.appendChild(hit); g.appendChild(ring); g.appendChild(dot); g.appendChild(gl); g.appendChild(lab);
    g.addEventListener('click', function () {
      if (routeMode) { route.push(p.id); drawRoute(); } else selectPoi(p.id); });
    g.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault();
        if (routeMode) { route.push(p.id); drawRoute(); } else selectPoi(p.id); } });
    gPois.appendChild(g);
    poiNodes[p.id] = g; dots.push(dot); labels.push(lab); hits.push(hit); glyphs.push(gl); rings.push(ring);
  });

  // keep markers at constant screen size as we zoom (r,font scale with viewBox)
  function updateSizes(r) {
    for (var i = 0; i < dots.length; i++) {
      var gp = poiById[M.pois[i].id];
      dots[i].setAttribute('r', (6 * r).toFixed(2));
      dots[i].setAttribute('stroke-width', (2.2 * r).toFixed(2));
      hits[i].setAttribute('r', (15 * r).toFixed(2));
      labels[i].setAttribute('font-size', (13 * r).toFixed(2));
      labels[i].setAttribute('x', (gp.x + 9 * r).toFixed(2));
      labels[i].setAttribute('y', (gp.y - 8 * r).toFixed(2));
      labels[i].setAttribute('stroke-width', (3 * r).toFixed(2));
      glyphs[i].setAttribute('transform', 'translate(' + gp.x + ',' + gp.y + ') scale(' + (r * 0.8).toFixed(3) + ')');
      glyphs[i].setAttribute('stroke-width', '1.1');
      rings[i].setAttribute('r', (11 * r).toFixed(2));
      rings[i].setAttribute('stroke-width', (2 * r).toFixed(2));
    }
    for (var j = 0; j < distLabels.length; j++)
      distLabels[j].setAttribute('font-size', (15 * r).toFixed(2));
    declutter();
  }

  // Greedy label declutter: show names in priority order (Notable first),
  // hiding any whose box overlaps one already shown. Recomputed on zoom, so
  // more names surface as you zoom in. Only runs when "Noms" is active.
  var poiOrder = M.pois.slice().sort(function (a, b) {
    return ((a.importance === 'Mineur') ? 1 : 0) - ((b.importance === 'Mineur') ? 1 : 0);
  });
  function declutter() {
    if (!svg.classList.contains('show-names')) return;
    var r = view.w / W0, placed = [];
    for (var k = 0; k < poiOrder.length; k++) {
      var p = poiOrder[k], node = poiNodes[p.id];
      if (node.classList.contains('filtered-out')) { node.classList.remove('lbl-on'); continue; }
      var fs = 13 * r, x0 = p.x + 9 * r, w = (p.name.length || 1) * fs * 0.52;
      var rc = [x0, p.y - 8 * r - fs, x0 + w, p.y - 8 * r + 2 * r], hit = false;
      for (var m = 0; m < placed.length; m++) {
        var q = placed[m];
        if (!(rc[2] < q[0] || rc[0] > q[2] || rc[3] < q[1] || rc[1] > q[3])) { hit = true; break; }
      }
      if (hit) node.classList.remove('lbl-on');
      else { node.classList.add('lbl-on'); placed.push(rc); }
    }
  }

  // ---- pan / zoom (manipulate the viewBox) ----
  function clampView() {
    var margin = 0.25;
    view.w = Math.min(view.w, W0 * (1 + margin));
    view.h = view.w * (H0 / W0);
    view.x = Math.max(-W0 * margin, Math.min(view.x, W0 - view.w + W0 * margin));
    view.y = Math.max(-H0 * margin, Math.min(view.y, H0 - view.h + H0 * margin));
  }
  function zoomAt(cx, cy, factor) {
    var rect = svg.getBoundingClientRect();
    var fx = (cx - rect.left) / rect.width, fy = (cy - rect.top) / rect.height;
    var wx = view.x + fx * view.w, wy = view.y + fy * view.h;
    var minW = W0 / 8, maxW = W0;
    var nw = Math.max(minW, Math.min(maxW, view.w / factor));
    var nh = nw * (H0 / W0);
    view.x = wx - fx * nw; view.y = wy - fy * nh; view.w = nw; view.h = nh;
    clampView(); applyView();
  }
  function focusOn(x, y, w) {
    w = w || W0 * 0.4; var h = w * (H0 / W0);
    view.x = x - w / 2; view.y = y - h / 2; view.w = w; view.h = h;
    clampView(); applyView();
  }
  svg.addEventListener('wheel', function (e) {
    e.preventDefault();
    zoomAt(e.clientX, e.clientY, e.deltaY < 0 ? 1.18 : 1 / 1.18);
  }, { passive: false });

  // Pan (1 pointer) + pinch-zoom (2 pointers, mobile). Pointer events unify
  // touch + mouse; #carte-svg has touch-action:none so the browser doesn't steal
  // the gesture. Only capture the pointer AFTER real movement, so a plain tap
  // still reaches the POI (capturing on pointerdown would retarget the click).
  var pdown = false, dragging = false, moved = false, captured = false;
  var pid = null, downX = 0, downY = 0, lastX = 0, lastY = 0;
  var pointers = {}, pinchDist = 0;
  function ptrPts() { return Object.keys(pointers).map(function (k) { return pointers[k]; }); }
  function ptrDist() { var p = ptrPts(); return Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y); }
  function ptrMid() { var p = ptrPts(); return [(p[0].x + p[1].x) / 2, (p[0].y + p[1].y) / 2]; }
  svg.addEventListener('pointerdown', function (e) {
    pointers[e.pointerId] = { x: e.clientX, y: e.clientY };
    if (Object.keys(pointers).length === 2) {   // enter pinch: cancel any single-pan
      pdown = false;
      if (captured) { try { svg.releasePointerCapture(pid); } catch (_) {} captured = false; }
      if (dragging) { dragging = false; svg.classList.remove('grabbing'); }
      pinchDist = ptrDist(); moved = true;
      return;
    }
    pdown = true; dragging = false; moved = false; captured = false; pid = e.pointerId;
    downX = lastX = e.clientX; downY = lastY = e.clientY;
  });
  svg.addEventListener('pointermove', function (e) {
    if (pointers[e.pointerId]) { pointers[e.pointerId].x = e.clientX; pointers[e.pointerId].y = e.clientY; }
    if (Object.keys(pointers).length >= 2) {     // pinch-zoom around the fingers' midpoint
      var nd = ptrDist();
      if (pinchDist > 0 && nd > 0) { var mid = ptrMid(); zoomAt(mid[0], mid[1], nd / pinchDist); }
      pinchDist = nd; moved = true;
      return;
    }
    if (!pdown) return;
    if (!dragging) {
      if (Math.abs(e.clientX - downX) + Math.abs(e.clientY - downY) < 4) return;
      dragging = true; moved = true; svg.classList.add('grabbing');
      try { svg.setPointerCapture(pid); captured = true; } catch (_) {}
    }
    var rect = svg.getBoundingClientRect();
    var dx = (e.clientX - lastX) / rect.width * view.w;
    var dy = (e.clientY - lastY) / rect.height * view.h;
    view.x -= dx; view.y -= dy; lastX = e.clientX; lastY = e.clientY;
    clampView(); applyView();
  });
  function endDrag(e) {
    if (e && pointers[e.pointerId]) delete pointers[e.pointerId];
    if (Object.keys(pointers).length < 2) pinchDist = 0;
    pdown = false;
    if (captured) { try { svg.releasePointerCapture(pid); } catch (_) {} captured = false; }
    if (dragging) { dragging = false; svg.classList.remove('grabbing'); }
  }
  svg.addEventListener('pointerup', endDrag);
  svg.addEventListener('pointercancel', endDrag);
  // swallow click after a drag so we don't accidentally select on pan-release
  gPois.addEventListener('click', function (e) { if (moved) { e.stopPropagation(); } }, true);

  // ---- panel ----
  var panel = document.getElementById('carte-panel');
  var selectedId = null;
  function selectPoi(id) {
    var p = poiById[id]; if (!p) return;
    selectedId = id;
    for (var k in poiNodes) poiNodes[k].classList.remove('selected');
    poiNodes[id].classList.add('selected');
    var h = '<h3>' + esc(p.name) + '</h3>';
    var meta = [];
    var tl = (M.types || []).find(function (t) { return t.key === p.type; });
    if (tl) meta.push(esc(tl.label));
    if (p.importance) meta.push(esc(p.importance));
    if (p.approx) meta.push('position approchée');
    if (meta.length) h += '<span class="carte-zone-tag">' + meta.join(' · ') + '</span>';
    if (p.section) h += '<div class="carte-quarter-tag">Section : ' + esc(sectionLabel(p.section))
      + (p.quartier ? ' · Quartier : ' + esc(p.quartier) : '') + '</div>';
    if (p.desc) h += '<p class="carte-desc">' + esc(p.desc) + '</p>';
    if (p.ficheUrl) h += '<a class="carte-fiche-link" href="' + esc(p.ficheUrl) + '">Fiche du lieu →</a>';
    // Scenes are shown ONLY for the currently selected scenario (nothing if "aucun").
    var cur = document.getElementById('carte-scenario').value;
    var refs = cur && p.scenarios ? p.scenarios[cur] : null;
    if (refs && refs.length) {
      h += '<h4>' + esc(cur) + '</h4><ul class="carte-scenes">';
      refs.forEach(function (ref) {
        h += ref.url ? '<li><a href="' + esc(ref.url) + '">' + esc(ref.label) + '</a></li>'
                     : '<li>' + esc(ref.label) + '</li>';
      });
      h += '</ul>';
    }
    panel.innerHTML = h;
  }

  function sectionLabel(key) {
    var s = (M.sections || []).find(function (x) { return x.key === key; });
    return s ? s.label : key;
  }
  function bindPanelLinks() {
    panel.querySelectorAll('[data-poi]').forEach(function (a) {
      a.addEventListener('click', function () { selectPoi(a.getAttribute('data-poi')); }); });
    panel.querySelectorAll('[data-quarter]').forEach(function (a) {
      a.addEventListener('click', function () { showQuarterByName(a.getAttribute('data-quarter')); }); });
  }
  function showSection(key) {
    for (var k in poiNodes) poiNodes[k].classList.remove('selected');
    selectedId = null;
    var meta = (M.sections || []).find(function (x) { return x.key === key; });
    var h = '<h3>' + esc(meta ? meta.label : key) + '</h3><span class="carte-zone-tag">Section</span>';
    if (meta && meta.desc) h += '<p class="carte-desc">' + esc(meta.desc) + '</p>';
    var quartiers = (M.quarterPolygons && M.quarterPolygons.length
        ? M.quarterPolygons.filter(function (q) { return q.section === key; })
                           .map(function (q) { return q.name; })
        : seeds.filter(function (s) { return s.ref.section === key; })
               .map(function (s) { return s.name; })).sort();
    if (quartiers.length) {
      h += '<h4>Quartiers</h4><ul class="carte-poi-list">';
      quartiers.forEach(function (n) { h += '<li><a data-quarter="' + esc(n) + '">' + esc(n) + '</a></li>'; });
      h += '</ul>';
    }
    panel.innerHTML = h; bindPanelLinks();
  }
  function showQuarter(seed) {
    if (!seed) return;
    if (seed.kind === 'poi') { selectPoi(seed.ref.id); return; }  // a quarter-POI is a place
    for (var k in poiNodes) poiNodes[k].classList.remove('selected');
    selectedId = null;
    var dz = seed.ref;
    var h = '<h3>' + esc(dz.name) + '</h3>'
          + '<span class="carte-zone-tag">Quartier · ' + esc(sectionLabel(dz.section)) + '</span>';
    if (dz.desc) h += '<p class="carte-desc">' + esc(dz.desc) + '</p>';
    h += liveZoneNote(dz.section);
    panel.innerHTML = h;
  }

  // ---- scenario selector ----
  var sel = document.getElementById('carte-scenario');
  var toggle = document.getElementById('carte-toggle-scenario');
  var hubLink = document.getElementById('carte-hub-link');
  var scenNames = {};
  (M.scenarios || []).forEach(function (s) { scenNames[s.name] = s; });
  M.pois.forEach(function (p) { Object.keys(p.scenarios || {}).forEach(function (nm) {
    if (!scenNames[nm]) scenNames[nm] = { name: nm }; }); });
  Object.keys(scenNames).forEach(function (nm) {
    var o = document.createElement('option'); o.value = nm; o.textContent = nm; sel.appendChild(o); });
  function poiHasScenario(p, nm) { return p.scenarios && p.scenarios[nm] && p.scenarios[nm].length > 0; }
  function applyScenario() {
    var nm = sel.value, hideOthers = toggle.checked;
    M.pois.forEach(function (p) {
      var node = poiNodes[p.id]; node.classList.remove('has-scenario', 'dim');
      if (!nm) return;
      if (poiHasScenario(p, nm)) node.classList.add('has-scenario');
      else if (hideOthers) node.classList.add('dim');
    });
    var meta = scenNames[nm];
    if (nm && meta && meta.hubUrl) { hubLink.href = meta.hubUrl; hubLink.hidden = false;
      hubLink.textContent = 'Hub : ' + nm + ' →'; } else { hubLink.hidden = true; }
    applySearch();
    syncLive();
    if (selectedId) selectPoi(selectedId);   // re-render panel for the new scenario
  }
  sel.addEventListener('change', applyScenario);
  toggle.addEventListener('change', applyScenario);

  // ---- Écran live (#7): heure × zone × variante → ce qui se passe autour des PJ ----
  var LIVE = M.scenarioLive || {};
  var liveEl = document.getElementById('carte-live');
  var liveHourSel = document.getElementById('carte-live-hour');
  var liveVarsEl = document.getElementById('carte-live-vars');
  var liveShowBtn = document.getElementById('carte-live-show');
  var activeVars = {};            // B/C/D heat flags (A = baseline)
  var liveScenario = null;
  function syncLive() {
    var lv = LIVE[sel.value];
    liveEl.hidden = !lv;
    if (!lv) { liveScenario = null; return; }
    if (liveScenario !== sel.value) {       // (re)build controls only on scenario change
      liveScenario = sel.value;
      liveHourSel.innerHTML = lv.hours.map(function (H, i) {
        return '<option value="' + i + '">' + esc(H.label) + '</option>'; }).join('');
      activeVars = {};
      liveVarsEl.innerHTML = lv.variantes.filter(function (v) { return v.key !== 'A'; })
        .map(function (v) { return '<button class="carte-chip" data-var="' + v.key + '">'
          + v.key + ' · ' + esc(v.label) + '</button>'; }).join('');
      liveVarsEl.querySelectorAll('[data-var]').forEach(function (b) {
        b.addEventListener('click', function () {
          var k = b.getAttribute('data-var'); activeVars[k] = !activeVars[k];
          b.classList.toggle('active', activeVars[k]); renderLive(); }); });
    }
  }
  function liveZoneNote(section) {
    var lv = LIVE[sel.value]; if (!lv) return '';
    var wealth = (lv.wealthBySection || {})[section]; if (!wealth) return '';
    var H = lv.hours[+liveHourSel.value || 0];
    return '<p class="carte-desc"><strong>Écran live · zone ' + esc(wealth) + '</strong> ('
      + esc(H.label) + ') : ' + esc(wealth === 'pauvre' ? H.pauvre : H.riche) + '</p>';
  }
  function renderLive() {
    var lv = LIVE[sel.value]; if (!lv) return;
    var H = lv.hours[+liveHourSel.value || 0];
    var anyHeat = activeVars.B || activeVars.C || activeVars.D;
    var h = '<h3>Écran live</h3><span class="carte-zone-tag">' + esc(H.label) + '</span>'
      + '<h4>Dans la rue</h4>'
      + '<p class="carte-desc"><strong>Riches</strong> (rive sud) : ' + esc(H.riche) + '</p>'
      + '<p class="carte-desc"><strong>Pauvres</strong> (Reikerbahn / docks) : ' + esc(H.pauvre) + '</p>'
      + '<h4>Rumeurs</h4><p class="carte-desc">Période : ' + esc(H.rumeur) + '.</p>';
    if (H.clock && H.clock.length)
      h += '<h4>Horloge</h4><ul class="carte-poi-list">'
        + H.clock.map(function (c) { return '<li>' + esc(c) + '</li>'; }).join('') + '</ul>';
    var tq = H.traque || {}, tl = [];
    if (tq.base) tl.push(tq.base);
    ['B', 'C', 'D'].forEach(function (k) { if (activeVars[k] && tq[k]) tl.push('[' + k + '] ' + tq[k]); });
    if (tl.length)
      h += '<h4>Traque</h4><ul class="carte-poi-list">'
        + tl.map(function (t) { return '<li>' + esc(t) + '</li>'; }).join('') + '</ul>';
    var defs = lv.variantes.filter(function (v) {
      return v.key === 'A' ? !anyHeat : activeVars[v.key]; });
    if (defs.length) {
      h += '<h4>Variante' + (defs.length > 1 ? 's' : '') + '</h4>';
      defs.forEach(function (v) { h += '<p class="carte-desc"><strong>' + v.key + ' — '
        + esc(v.label) + '</strong> : ' + esc(v.desc) + '</p>'; });
    }
    panel.innerHTML = h;
  }
  liveHourSel.addEventListener('change', renderLive);
  liveShowBtn.addEventListener('click', renderLive);

  // ---- search ----
  var searchInput = document.getElementById('carte-search');
  function applySearch() {
    var q = norm(searchInput.value.trim());
    M.pois.forEach(function (p) {
      var node = poiNodes[p.id]; node.classList.remove('search-hit');
      if (q && norm(p.name).indexOf(q) !== -1) node.classList.add('search-hit');
    });
  }
  searchInput.addEventListener('input', applySearch);

  // ---- filters: type (chips) + importance ----
  var activeTypes = {}; (M.types || []).forEach(function (t) { activeTypes[t.key] = true; });
  var showImp = { Notable: true, Mineur: true };
  var filtersEl = document.getElementById('carte-filters');
  function poiVisible(p) {
    if (!activeTypes[p.type || 'autre']) return false;
    if (!showImp[p.importance || 'Notable']) return false;
    return true;
  }
  function applyFilters() {
    M.pois.forEach(function (p) {
      poiNodes[p.id].classList.toggle('filtered-out', !poiVisible(p));
    });
    declutter();
  }
  if (filtersEl) {
    var h = '<span class="carte-filter-lbl">Types</span>';
    (M.types || []).forEach(function (t) {
      h += '<button class="carte-chip active" data-type="' + esc(t.key) + '">' + esc(t.label) + '</button>'; });
    h += '<span class="carte-filter-lbl">Importance</span>'
       + '<button class="carte-chip active" data-imp="Notable">Notable</button>'
       + '<button class="carte-chip active" data-imp="Mineur">Mineur</button>';
    filtersEl.innerHTML = h;
    filtersEl.querySelectorAll('[data-type]').forEach(function (b) {
      b.addEventListener('click', function () {
        var k = b.getAttribute('data-type'); activeTypes[k] = !activeTypes[k];
        b.classList.toggle('active', activeTypes[k]); applyFilters(); }); });
    filtersEl.querySelectorAll('[data-imp]').forEach(function (b) {
      b.addEventListener('click', function () {
        var k = b.getAttribute('data-imp'); showImp[k] = !showImp[k];
        b.classList.toggle('active', showImp[k]); applyFilters(); }); });
  }
  applyFilters();

  // ---- pathfinding (#6): REAL graph Dijkstra over the quarter graph ----
  // Graph nodes = the 33 canon quarters (their anchor centroids), independent of
  // which quarters have a fiche. Falls back to fiche-seeds if no polygons.
  var QNODE = {};
  if (M.quarterPolygons && M.quarterPolygons.length)
    M.quarterPolygons.forEach(function (q) { QNODE[q.name] = { x: q.cx, y: q.cy }; });
  else seeds.forEach(function (s) { QNODE[s.name] = { x: s.x, y: s.y }; });
  function nearestSeedName(x, y) {
    var best = null, bd = Infinity;
    Object.keys(QNODE).forEach(function (n) { var p = QNODE[n];
      var d = (p.x - x) * (p.x - x) + (p.y - y) * (p.y - y);
      if (d < bd) { bd = d; best = n; } });
    return best;
  }
  // River-aware quarter graph from embedded edges. Land edges follow real
  // polygon adjacency (never cross water); bridge edges carry a `via` crossing
  // point so a route bends THROUGH the bridge instead of cutting open water.
  var SADJ = {}, VIA = {};
  Object.keys(QNODE).forEach(function (n) { SADJ[n] = []; });
  (M.edges || []).forEach(function (e) {
    var A = QNODE[e.a], Bs = QNODE[e.b];
    if (!A || !Bs) return;
    var w;
    if (e.bridge && e.via) {
      w = Math.hypot(A.x - e.via[0], A.y - e.via[1]) + Math.hypot(e.via[0] - Bs.x, e.via[1] - Bs.y) + 30;
      VIA[e.a + '|' + e.b] = e.via; VIA[e.b + '|' + e.a] = e.via;
    } else { w = Math.hypot(A.x - Bs.x, A.y - Bs.y); }
    SADJ[e.a].push([e.b, w]); SADJ[e.b].push([e.a, w]);
  });
  function dijkstra(src, dst) {
    if (src === dst) return [src];
    var D = {}, prev = {}, done = {}, pq = [[0, src]];
    D[src] = 0;
    while (pq.length) {
      pq.sort(function (a, b) { return a[0] - b[0]; });
      var top = pq.shift(), d = top[0], u = top[1];
      if (done[u]) continue; done[u] = 1;
      if (u === dst) break;
      (SADJ[u] || []).forEach(function (e) {
        var v = e[0], nd = d + e[1];
        if (D[v] == null || nd < D[v]) { D[v] = nd; prev[v] = u; pq.push([nd, v]); }
      });
    }
    if (D[dst] == null) return null;
    var path = [dst], c = dst;
    while (c !== src) { c = prev[c]; if (c == null) return null; path.unshift(c); }
    return path;
  }
  function drawRoute() {
    while (gRoute.firstChild) gRoute.removeChild(gRoute.firstChild);
    var pts = route.map(function (id) { return poiById[id]; }).filter(Boolean);
    var line = [], quarters = [];
    for (var i = 0; i < pts.length - 1; i++) {
      var a = pts[i], b = pts[i + 1];
      var qa = nearestSeedName(a.x, a.y), qb = nearestSeedName(b.x, b.y);
      var qpath = dijkstra(qa, qb) || [qa, qb];
      if (i === 0) line.push([a.x, a.y]);
      for (var kk = 0; kk < qpath.length; kk++) {
        var qn = qpath[kk];
        if (kk > 0) { var v = VIA[qpath[kk - 1] + '|' + qn]; if (v) line.push([v[0], v[1]]); }
        var nd = QNODE[qn]; if (nd) line.push([nd.x, nd.y]);
        if (quarters[quarters.length - 1] !== qn) quarters.push(qn);
      }
      line.push([b.x, b.y]);
    }
    if (line.length >= 2)
      gRoute.appendChild(el('polyline', { points: line.map(function (p) { return p[0].toFixed(1) + ',' + p[1].toFixed(1); }).join(' '),
        'class': 'carte-route' }));
    pts.forEach(function (p) { gRoute.appendChild(el('circle', { cx: p.x, cy: p.y, r: 5, 'class': 'carte-route-pt' })); });
    var seen = {}, qd = quarters.filter(function (q) { if (seen[q]) return false; seen[q] = 1; return true; });
    var h = '<h3>Trajet</h3><span class="carte-zone-tag">' + pts.length + ' point(s)</span>';
    if (!pts.length) h += '<p class="carte-panel-empty">Clique des lieux pour tracer un trajet (Dijkstra par quartiers).</p>';
    if (pts.length) h += '<h4>Étapes</h4><ul class="carte-poi-list">'
      + pts.map(function (p) { return '<li>' + esc(p.name) + '</li>'; }).join('') + '</ul>';
    if (qd.length) h += '<h4>Quartiers traversés (' + qd.length + ')</h4><ul class="carte-poi-list">'
      + qd.map(function (q) { return '<li>' + esc(q) + '</li>'; }).join('') + '</ul>';
    panel.innerHTML = h;
  }
  var routeBtn = document.getElementById('carte-route-toggle');
  if (routeBtn) routeBtn.addEventListener('click', function () {
    routeMode = !routeMode; route = [];
    routeBtn.classList.toggle('active', routeMode);
    while (gRoute.firstChild) gRoute.removeChild(gRoute.firstChild);
    svg.classList.toggle('route-mode', routeMode);
    if (routeMode) { panel.innerHTML = '<h3>Trajet</h3><p class="carte-panel-empty">'
      + 'Mode trajet activé. Clique des lieux dans l\'ordre ; les quartiers traversés s\'affichent. '
      + 'Re-clique « Trajet » pour effacer.</p>'; }
    else { panel.innerHTML = '<p class="carte-panel-empty">Clique un lieu sur la carte.</p>'; }
  });

  // ---- "Noms" toggle + reset view ----
  var namesToggle = document.getElementById('carte-toggle-names');
  if (namesToggle) namesToggle.addEventListener('change', function () {
    svg.classList.toggle('show-names', namesToggle.checked);
    declutter();
  });
  var zonesToggle = document.getElementById('carte-toggle-zones');
  if (zonesToggle) zonesToggle.addEventListener('change', function () {
    svg.classList.toggle('hide-zones', !zonesToggle.checked);
  });
  var resetBtn = document.getElementById('carte-reset');
  if (resetBtn) resetBtn.addEventListener('click', function () {
    view = { x: 0, y: 0, w: W0, h: H0 }; applyView();
  });

  applyView();
})();
