// MJ mode toggle: ?mj=TOKEN sets localStorage flag; ?mj=off clears.
// When the flag matches the build-time token, body.mj-mode is applied
// and CSS reveals .mj-only sections + adds a corner badge.
(function() {
  var TOKEN = "7e3a91d5c2f48b6a";
  var p = new URLSearchParams(window.location.search);
  var fromUrl = p.get('mj');
  function cleanUrl() {
    p.delete('mj');
    var s = p.toString();
    var u = window.location.pathname + (s ? '?' + s : '') + window.location.hash;
    window.history.replaceState({}, '', u);
  }
  if (fromUrl === TOKEN) {
    localStorage.setItem('mjMode', TOKEN);
    cleanUrl();
  } else if (fromUrl === 'off') {
    localStorage.removeItem('mjMode');
    cleanUrl();
  }

  function activate() {
    document.documentElement.classList.add('mj-mode');
    if (!document.body) return;
    document.body.classList.add('mj-mode');
    if (document.getElementById('mj-mode-badge')) return;
    var b = document.createElement('div');
    b.id = 'mj-mode-badge';
    b.innerHTML = '<span>Mode MJ</span><button title="Quitter le mode MJ" aria-label="Quitter">×</button>';
    b.querySelector('button').addEventListener('click', function() {
      localStorage.removeItem('mjMode');
      location.reload();
    });
    document.body.appendChild(b);
  }

  if (localStorage.getItem('mjMode') === TOKEN) {
    if (document.body) document.body.dataset.mjToken = TOKEN;
    activate();
    if (!document.body) {
      document.addEventListener('DOMContentLoaded', function() {
        document.body.dataset.mjToken = TOKEN;
        activate();
      });
    }
  }

  // -------- Canon ref popover (MJ-only) --------------------------------
  // Triggered by hover on <span class="canon-ref" data-source data-extract>.
  // Shows the cited markdown lines from the Source/ book tree.
  var cPop = null, cShowTimer = null, cHideTimer = null;

  function cEnsurePopover() {
    if (cPop) return cPop;
    cPop = document.createElement('div');
    cPop.className = 'canon-popover';
    cPop.addEventListener('mouseenter', function () {
      if (cHideTimer) { clearTimeout(cHideTimer); cHideTimer = null; }
    });
    cPop.addEventListener('mouseleave', cSchedulePopoverHide);
    (document.body || document.documentElement).appendChild(cPop);
    return cPop;
  }

  function cFillPopover(trigger) {
    var p = cEnsurePopover();
    var src     = trigger.getAttribute('data-source')  || trigger.textContent.trim();
    var extract = trigger.getAttribute('data-extract') || '';
    p.innerHTML = '';
    var hdr = document.createElement('div');
    hdr.className = 'canon-popover-header';
    hdr.textContent = src;
    p.appendChild(hdr);
    var body = document.createElement('div');
    body.className = 'canon-popover-body';
    body.textContent = extract;
    p.appendChild(body);
    p.style.display = 'block';
    cPositionPopover(p, trigger);
  }

  function cPositionPopover(p, trigger) {
    var rect = trigger.getBoundingClientRect();
    var pw = p.offsetWidth, ph = p.offsetHeight;
    var top  = rect.bottom + 8;                       // prefer below
    var left = rect.left + rect.width / 2 - pw / 2;
    if (top + ph > window.innerHeight - 8) {
      top = rect.top - ph - 8;                        // flip above if no room
      if (top < 8) top = 8;
    }
    if (left < 8) left = 8;
    var maxLeft = window.innerWidth - pw - 8;
    if (left > maxLeft) left = Math.max(8, maxLeft);
    p.style.top = top + 'px';
    p.style.left = left + 'px';
  }

  function cSchedulePopoverShow(trigger) {
    if (cHideTimer) { clearTimeout(cHideTimer); cHideTimer = null; }
    if (cShowTimer) clearTimeout(cShowTimer);
    cShowTimer = setTimeout(function () { cFillPopover(trigger); }, 150);
  }
  function cSchedulePopoverHide() {
    if (cShowTimer) { clearTimeout(cShowTimer); cShowTimer = null; }
    if (cHideTimer) clearTimeout(cHideTimer);
    cHideTimer = setTimeout(function () {
      if (cPop) cPop.style.display = 'none';
    }, 200);
  }

  document.addEventListener('mouseover', function (e) {
    var t = e.target.closest && e.target.closest('.canon-ref');
    if (t) cSchedulePopoverShow(t);
  });
  document.addEventListener('mouseout', function (e) {
    var t = e.target.closest && e.target.closest('.canon-ref');
    if (t) cSchedulePopoverHide();
  });
})();
