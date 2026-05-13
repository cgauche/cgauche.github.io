// Client-side search.  Loads search-index.json on first focus, then filters
// in-memory on every keystroke.  No deps.
(function () {
  var norm = function (s) {
    return s.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
  };
  var escapeHtml = function (s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  };

  var index = null, indexPromise = null;
  function loadIndex(base) {
    if (indexPromise) return indexPromise;
    indexPromise = fetch(base + 'search-index.json')
      .then(function (r) { return r.json(); })
      .then(function (data) { index = data; return data; });
    return indexPromise;
  }

  function render(matches, base) {
    if (!matches.length) {
      return '<div class="search-empty">Aucun résultat.</div>';
    }
    return matches.slice(0, 14).map(function (m) {
      var e = m.entry;
      var thumb = e.i
        ? '<img class="search-thumb" src="' + escapeHtml(e.i) + '" alt="" loading="lazy">'
        : '<span class="search-thumb search-thumb-fallback">' +
          escapeHtml(e.n != null ? String(e.n).padStart(2, '0') : (e.t.charAt(0) || '·')) +
          '</span>';
      var cat = e.c ? '<span class="search-cat">' + escapeHtml(e.c) + '</span>' : '';
      return '<a class="search-result" href="' + escapeHtml(base + e.u) + '">' +
             thumb +
             '<span class="search-meta"><span class="search-title">' + escapeHtml(e.t) + '</span>' + cat + '</span>' +
             '</a>';
    }).join('');
  }

  // --- Single source of truth for ranking & filtering -------------------
  // Change MIN_SCORE here (or the score thresholds below) and both the
  // dropdown and the /search/ results page pick it up.
  var MIN_SCORE = 50;
  function score(entry, qNorm) {
    var titleN = norm(entry.t);
    if (titleN === qNorm) return 100;             // exact title
    if (titleN.startsWith(qNorm)) return 80;      // title-prefix
    if (titleN.indexOf(qNorm) >= 0) return 60;    // title contains
    if (entry.vt) {                               // variant-title match
      for (var i = 0; i < entry.vt.length; i++) {
        if (entry.vt[i].indexOf(qNorm) >= 0) return 50;
      }
    }
    if (entry.s.indexOf(qNorm) >= 0) return 20;   // body / haystack
    return 0;
  }
  function findMatches(qNorm) {
    var out = [];
    for (var i = 0; i < index.length; i++) {
      var sc = score(index[i], qNorm);
      if (sc >= MIN_SCORE) out.push({ entry: index[i], score: sc });
    }
    out.sort(function (a, b) {
      if (b.score !== a.score) return b.score - a.score;
      return a.entry.t.localeCompare(b.entry.t, 'fr');
    });
    return out;
  }

  function attach(input, results, base) {
    var open = function () { results.classList.add('is-open'); };
    var close = function () { results.classList.remove('is-open'); };

    input.addEventListener('focus', function () { loadIndex(base); });

    input.addEventListener('input', function () {
      var q = input.value.trim();
      if (q.length < 2) { close(); results.innerHTML = ''; return; }
      var qN = norm(q);
      loadIndex(base).then(function () {
        var matches = findMatches(qN);
        results.innerHTML = render(matches, base);
        open();
      });
    });

    input.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { close(); input.blur(); }
      if (e.key === 'Enter') {
        var q = input.value.trim();
        if (q) {
          // Full results page rather than jumping to the top match.
          window.location.href = base + 'search/?q=' + encodeURIComponent(q);
        }
      }
    });

    document.addEventListener('click', function (e) {
      if (!input.contains(e.target) && !results.contains(e.target)) close();
    });
  }

  // ---------- Search results PAGE (`/search/?q=...`) -------------------

  var CATEGORY_ORDER = ['Résumés', 'PJ', 'PNJ', 'Lieux', 'Documents', 'Univers'];

  function renderSearchPage() {
    var container = document.getElementById('search-page-results');
    var titleEl   = document.getElementById('search-page-query');
    if (!container || !titleEl) return;

    var base = container.dataset.base || '';
    var params = new URLSearchParams(window.location.search);
    var q = (params.get('q') || '').trim();

    // Mirror the query in the page-top input so the user can refine it.
    var pageInput = document.querySelector('.search-input');
    if (pageInput) pageInput.value = q;

    if (!q) {
      titleEl.textContent = 'Tapez une requête dans la barre du haut.';
      return;
    }

    titleEl.innerHTML = 'Résultats pour « <em>' + escapeHtml(q) + '</em> »';

    var qN = norm(q);
    loadIndex(base).then(function () {
      var matches = findMatches(qN);
      if (!matches.length) {
        container.innerHTML = '<p class="search-empty">Aucun résultat.</p>';
        return;
      }

      var byCat = {};
      matches.forEach(function (m) {
        var c = m.entry.c || 'Autres';
        (byCat[c] = byCat[c] || []).push(m);
      });
      // categories not in CATEGORY_ORDER go at the end, alphabetical
      var order = CATEGORY_ORDER.slice();
      Object.keys(byCat).forEach(function (c) {
        if (order.indexOf(c) === -1) order.push(c);
      });

      var out = [];
      order.forEach(function (cat) {
        var list = byCat[cat];
        if (!list) return;
        out.push('<section class="search-group">');
        out.push('<h2 class="search-group-title">' + escapeHtml(cat) +
                 '<span class="count">' + list.length + '</span></h2>');
        out.push('<ul class="card-grid card-grid-entries">');
        list.forEach(function (m) {
          var e = m.entry;
          var thumb = e.i
            ? '<div class="thumb-wrap"><img class="thumb" loading="lazy" src="' +
              escapeHtml(e.i) + '" alt=""></div>'
            : '<div class="thumb-wrap thumb-fallback"><span>' +
              escapeHtml(e.n != null ? String(e.n).padStart(2, '0')
                                       : (e.t.charAt(0) || '·')) +
              '</span></div>';
          out.push('<li><a class="thumb-card entry-card" href="' +
                   escapeHtml(base + e.u) + '">' + thumb +
                   '<div class="thumb-card-body"><span class="entry-name">' +
                   escapeHtml(e.t) + '</span></div></a></li>');
        });
        out.push('</ul></section>');
      });
      container.innerHTML = out.join('');
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    var input = document.querySelector('.search-input');
    var results = document.querySelector('.search-results');
    if (input && results) attach(input, results, input.dataset.base || '');
    renderSearchPage();
  });

  // ---------- Image lightbox -------------------------------------------
  // Blogger wraps post images in <a href="...full-size.webp"><img></a>.
  // Without intervention, clicking navigates away from the site to a raw
  // image URL. Intercept the click and show the image in an in-page modal.

  var IMG_EXT = /\.(png|jpe?g|gif|webp|svg|avif)(\?.*)?$/i;

  function openLightbox(href, alt) {
    var overlay = document.createElement('div');
    overlay.className = 'lightbox-overlay';
    var img = document.createElement('img');
    img.className = 'lightbox-img';
    img.src = href;
    img.alt = alt || '';
    var btn = document.createElement('button');
    btn.className = 'lightbox-close';
    btn.setAttribute('aria-label', 'Fermer');
    btn.innerHTML = '&times;';
    overlay.appendChild(img);
    overlay.appendChild(btn);
    document.body.appendChild(overlay);
    document.body.classList.add('lightbox-open');

    function close() {
      overlay.remove();
      document.body.classList.remove('lightbox-open');
      document.removeEventListener('keydown', onKey);
    }
    function onKey(e) { if (e.key === 'Escape') close(); }

    overlay.addEventListener('click', function (e) {
      // Click on the image itself shouldn't close; clicking elsewhere does.
      if (e.target !== img) close();
    });
    document.addEventListener('keydown', onKey);
  }

  document.addEventListener('click', function (e) {
    var img = e.target;
    if (!img || img.tagName !== 'IMG') return;
    var anchor = img.closest('a');
    if (!anchor) return;
    var href = anchor.getAttribute('href');
    if (!href || !IMG_EXT.test(href)) return;
    e.preventDefault();
    openLightbox(href, img.getAttribute('alt'));
  });

  // ---------- Entity popovers -----------------------------------------
  // Triggered by hover on <a class="entity-pop" data-portrait="…">. Mobile
  // devices fall back to plain link navigation (CSS hides the popover via
  // `@media (hover: none)`).

  var popover = null, showTimer = null, hideTimer = null;

  function ensurePopover() {
    if (popover) return popover;
    popover = document.createElement('div');
    popover.className = 'entity-popover';
    popover.addEventListener('mouseenter', function () {
      if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
    });
    popover.addEventListener('mouseleave', schedulePopoverHide);
    document.body.appendChild(popover);
    return popover;
  }

  function fillPopover(trigger) {
    var p = ensurePopover();
    var portrait = trigger.getAttribute('data-portrait');
    var name = trigger.textContent.trim();
    p.innerHTML = '';
    if (portrait) {
      var img = document.createElement('img');
      img.src = portrait;
      img.alt = '';
      img.loading = 'lazy';
      p.appendChild(img);
    }
    var label = document.createElement('span');
    label.className = 'entity-popover-name';
    label.textContent = name;
    p.appendChild(label);
    p.style.display = 'flex';
    positionPopover(p, trigger);
  }

  function positionPopover(p, trigger) {
    var rect = trigger.getBoundingClientRect();
    var pw = p.offsetWidth, ph = p.offsetHeight;
    var top  = rect.top - ph - 8;
    var left = rect.left + rect.width / 2 - pw / 2;
    if (top < 8) top = rect.bottom + 8;             // flip below if no room above
    if (left < 8) left = 8;
    var maxLeft = window.innerWidth - pw - 8;
    if (left > maxLeft) left = maxLeft;
    p.style.top = top + 'px';
    p.style.left = left + 'px';
  }

  function schedulePopoverShow(trigger) {
    if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
    if (showTimer) clearTimeout(showTimer);
    showTimer = setTimeout(function () { fillPopover(trigger); }, 150);
  }
  function schedulePopoverHide() {
    if (showTimer) { clearTimeout(showTimer); showTimer = null; }
    if (hideTimer) clearTimeout(hideTimer);
    hideTimer = setTimeout(function () {
      if (popover) popover.style.display = 'none';
    }, 200);
  }

  document.addEventListener('mouseover', function (e) {
    var t = e.target.closest && e.target.closest('.entity-pop');
    if (t) schedulePopoverShow(t);
  });
  document.addEventListener('mouseout', function (e) {
    var t = e.target.closest && e.target.closest('.entity-pop');
    if (t) schedulePopoverHide();
  });
})();
