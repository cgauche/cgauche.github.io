// Carte interactive (Étape A): poster canon en fond + zoom/pan,
// marqueurs aux coordonnées canon, clic POI -> panneau, scénario -> highlight, recherche.
(function () {
  var dataEl = document.getElementById('carte-data');
  var svg = document.getElementById('carte-svg');
  if (!dataEl || !svg) return;
  var M = JSON.parse(dataEl.textContent);
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
  function applyView() {
    svg.setAttribute('viewBox', view.x + ' ' + view.y + ' ' + view.w + ' ' + view.h);
    var r = view.w / W0;                       // <1 when zoomed in
    svg.style.setProperty('--mk', r);          // marker scale ratio
    updateSizes(r);
  }

  // layers (drawn in canon coordinates; the viewBox does the zoom/pan)
  // order = paint/hit order: poster < zones < district labels < pois < legend hotspots
  var gPoster = el('g'), gZones = el('g'), gRoute = el('g'), gDist = el('g'), gPois = el('g'),
      gLegendZones = el('g'), gLegend = el('g');
  svg.appendChild(gPoster); svg.appendChild(gZones); svg.appendChild(gRoute);
  svg.appendChild(gDist); svg.appendChild(gPois); svg.appendChild(gLegendZones); svg.appendChild(gLegend);
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
  seeds.forEach(function (s, i) {
    var poly = [[ZX, 0], [W0, 0], [W0, H0], [ZX, H0]];
    for (var j = 0; j < seeds.length && poly.length >= 3; j++) {
      if (j !== i) poly = clipHP(poly, { x: s.x, y: s.y }, { x: seeds[j].x, y: seeds[j].y });
    }
    if (poly.length < 3) return;
    var pts = poly.map(function (p) { return p[0].toFixed(1) + ',' + p[1].toFixed(1); }).join(' ');
    var sec = s.ref.section;
    var pg = el('polygon', { points: pts, 'class': 'carte-zone', 'data-section': sec,
      fill: SECTION_COLOR[sec] || '#8a7a5a' });
    var ti = el('title'); ti.textContent = s.name; pg.appendChild(ti);
    // a map cell = a QUARTER (click → quarter). Sections are clicked via the legend.
    pg.addEventListener('click', function () { showQuarter(s); });
    gZones.appendChild(pg);
  });

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

  // POI markers
  var dots = [], labels = [], hits = [];
  M.pois.forEach(function (p) {
    var g = el('g', { 'class': 'carte-poi', 'data-id': p.id, 'data-type': p.type || 'autre', tabindex: 0 });
    var hit = el('circle', { cx: p.x, cy: p.y, r: 14, fill: 'transparent', 'class': 'carte-poi-hit' });
    var dot = el('circle', { cx: p.x, cy: p.y, r: 6, 'class': 'carte-poi-dot' });
    var lab = el('text', { x: p.x + 9, y: p.y - 8, 'class': 'carte-poi-label' });
    lab.textContent = p.name;
    g.appendChild(hit); g.appendChild(dot); g.appendChild(lab);
    g.addEventListener('click', function () {
      if (routeMode) { route.push(p.id); drawRoute(); } else selectPoi(p.id); });
    g.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault();
        if (routeMode) { route.push(p.id); drawRoute(); } else selectPoi(p.id); } });
    gPois.appendChild(g);
    poiNodes[p.id] = g; dots.push(dot); labels.push(lab); hits.push(hit);
  });

  // keep markers at constant screen size as we zoom (r,font scale with viewBox)
  function updateSizes(r) {
    for (var i = 0; i < dots.length; i++) {
      dots[i].setAttribute('r', (6 * r).toFixed(2));
      dots[i].setAttribute('stroke-width', (2.2 * r).toFixed(2));
      hits[i].setAttribute('r', (15 * r).toFixed(2));
      labels[i].setAttribute('font-size', (13 * r).toFixed(2));
      labels[i].setAttribute('x', (poiById[M.pois[i].id].x + 9 * r).toFixed(2));
      labels[i].setAttribute('y', (poiById[M.pois[i].id].y - 8 * r).toFixed(2));
      labels[i].setAttribute('stroke-width', (3 * r).toFixed(2));
    }
    for (var j = 0; j < distLabels.length; j++)
      distLabels[j].setAttribute('font-size', (15 * r).toFixed(2));
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

  // Pan: only capture the pointer AFTER real movement, so a plain click still
  // reaches the POI (capturing on pointerdown would retarget the click to <svg>).
  var pdown = false, dragging = false, moved = false, captured = false;
  var pid = null, downX = 0, downY = 0, lastX = 0, lastY = 0;
  svg.addEventListener('pointerdown', function (e) {
    pdown = true; dragging = false; moved = false; captured = false; pid = e.pointerId;
    downX = lastX = e.clientX; downY = lastY = e.clientY;
  });
  svg.addEventListener('pointermove', function (e) {
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
      a.addEventListener('click', function () { showQuarter(seedByName[a.getAttribute('data-quarter')]); }); });
  }
  function showSection(key) {
    for (var k in poiNodes) poiNodes[k].classList.remove('selected');
    selectedId = null;
    var meta = (M.sections || []).find(function (x) { return x.key === key; });
    var h = '<h3>' + esc(meta ? meta.label : key) + '</h3><span class="carte-zone-tag">Section</span>';
    if (meta && meta.desc) h += '<p class="carte-desc">' + esc(meta.desc) + '</p>';
    var quartiers = seeds.filter(function (s) { return s.ref.section === key; })
                         .map(function (s) { return s.name; }).sort();
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
    if (selectedId) selectPoi(selectedId);   // re-render panel for the new scenario
  }
  sel.addEventListener('change', applyScenario);
  toggle.addEventListener('change', applyScenario);

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

  // ---- pathfinding (#6): route through POIs + quartiers traversés ----
  function nearestSeedName(x, y) {
    var best = null, bd = Infinity;
    seeds.forEach(function (s) { var d = (s.x - x) * (s.x - x) + (s.y - y) * (s.y - y);
      if (d < bd) { bd = d; best = s; } });
    return best ? best.name : null;
  }
  function drawRoute() {
    while (gRoute.firstChild) gRoute.removeChild(gRoute.firstChild);
    var pts = route.map(function (id) { return poiById[id]; }).filter(Boolean);
    if (pts.length >= 2)
      gRoute.appendChild(el('polyline', { points: pts.map(function (p) { return p.x + ',' + p.y; }).join(' '),
        'class': 'carte-route' }));
    pts.forEach(function (p) { gRoute.appendChild(el('circle', { cx: p.x, cy: p.y, r: 5, 'class': 'carte-route-pt' })); });
    // quartiers traversés (échantillonnage le long du trajet, plus-proche-graine)
    var qs = [];
    for (var i = 0; i < pts.length - 1; i++) {
      var a = pts[i], b = pts[i + 1];
      var steps = Math.max(2, Math.round(Math.hypot(b.x - a.x, b.y - a.y) / 12));
      for (var t = 0; t <= steps; t++) {
        var q = nearestSeedName(a.x + (b.x - a.x) * t / steps, a.y + (b.y - a.y) * t / steps);
        if (q && qs[qs.length - 1] !== q) qs.push(q);
      }
    }
    var seen = {}, qdedup = qs.filter(function (q) { if (seen[q]) return false; seen[q] = 1; return true; });
    var h = '<h3>Trajet</h3><span class="carte-zone-tag">' + pts.length + ' point(s)</span>';
    if (!pts.length) h += '<p class="carte-panel-empty">Clique des lieux pour tracer un trajet.</p>';
    if (pts.length) h += '<h4>Étapes</h4><ul class="carte-poi-list">'
      + pts.map(function (p) { return '<li>' + esc(p.name) + '</li>'; }).join('') + '</ul>';
    if (qdedup.length) h += '<h4>Quartiers traversés</h4><ul class="carte-poi-list">'
      + qdedup.map(function (q) { return '<li>' + esc(q) + '</li>'; }).join('') + '</ul>';
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
