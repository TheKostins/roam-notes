/* site.js -- тема, шторка, поиск, проводник, граф.  Без зависимостей.
   window.SITE = {root, id} задаёт страницу; без него (mockup.html) данные
   графа берутся из window.MOCK_GRAPH. */
(function () {
  'use strict';
  var h = document.documentElement;
  var SITE = window.SITE || { root: '', id: '' };
  var $ = function (s, el) { return (el || document).querySelector(s); };
  var $$ = function (s, el) { return Array.prototype.slice.call((el || document).querySelectorAll(s)); };

  /* ---------- theme ---------- */
  function curTheme() { return h.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'); }
  $$('.theme-toggle').forEach(function (b) {
    b.addEventListener('click', function () {
      var t = curTheme() === 'dark' ? 'light' : 'dark';
      h.dataset.theme = t; try { localStorage.setItem('theme', t); } catch (e) {}
      redrawGraphs();
    });
  });
  var pair = $('#pair');                       // только в макете
  if (pair) {
    pair.value = h.dataset.pair;
    pair.addEventListener('change', function () { try { localStorage.setItem('pair', pair.value); } catch (e) {} location.reload(); });
  }

  /* ---------- drawer (mobile) ---------- */
  var menu = $('#menu'), scrim = $('#scrim'), q = $('#q');
  function openNav() { document.body.classList.add('nav-open'); }
  function closeNav() { document.body.classList.remove('nav-open'); }
  if (menu) menu.addEventListener('click', function () { document.body.classList.toggle('nav-open'); });
  if (scrim) scrim.addEventListener('click', closeNav);
  var ms = $('#msearch');
  if (ms) ms.addEventListener('click', function () { openNav(); setTimeout(function () { q && q.focus(); }, 250); });
  document.addEventListener('keydown', function (e) {
    if (e.key === '/' && document.activeElement.tagName !== 'INPUT' && q) { e.preventDefault(); q.focus(); }
    if (e.key === 'Escape') { closeNav(); if (q && document.activeElement === q) { q.value = ''; showResults(null); q.blur(); } }
  });

  /* ---------- explorer: current page ---------- */
  var explorer = $('.explorer');
  if (explorer && SITE.id) {
    var cur = explorer.querySelector('a[data-id="' + SITE.id + '"]');
    if (cur) {
      cur.classList.add('current');
      var d = cur.parentElement;
      while (d && d !== explorer) { if (d.tagName === 'DETAILS') d.open = true; d = d.parentElement; }
      if (cur.scrollIntoView) setTimeout(function () { cur.scrollIntoView({ block: 'center' }); }, 0);
    }
  }
  // ссылка внутри summary ведёт на страницу, а не сворачивает группу
  $$('.explorer summary a').forEach(function (a) { a.addEventListener('click', function (e) { e.stopPropagation(); }); });

  /* ---------- search ---------- */
  var results = $('#results'), index = null, loading = null;
  function norm(s) { return s.toLowerCase().replace(/ё/g, 'е'); }
  function loadIndex() {
    if (index || loading) return loading;
    loading = fetch(SITE.root + 'search.json').then(function (r) { return r.json(); }).then(function (data) {
      index = data.map(function (d) { d.nt = norm(d.t); d.nx = norm(d.x || ''); d.ng = (d.g || []).join(' '); return d; });
      return index;
    }).catch(function () { index = []; return index; });
    return loading;
  }
  function score(d, words) {
    var s = 0;
    for (var i = 0; i < words.length; i++) {
      var w = words[i];
      if (d.nt === w) s += 50;
      else if (d.nt.indexOf(w) === 0) s += 20;
      else if (d.nt.indexOf(w) >= 0) s += 12;
      else if (d.ng.indexOf(w) >= 0) s += 6;
      else if (d.nx.indexOf(w) >= 0) s += 3;
      else return 0;
    }
    return s;
  }
  function snippet(d, words) {
    var x = d.x || '', i = -1;
    for (var k = 0; k < words.length && i < 0; k++) i = d.nx.indexOf(words[k]);
    if (i < 0) return x.slice(0, 110);
    var a = Math.max(0, i - 40), b = Math.min(x.length, i + 80);
    return (a > 0 ? '…' : '') + x.slice(a, b) + (b < x.length ? '…' : '');
  }
  function mark(text, words) {
    var out = text;
    words.forEach(function (w) {
      if (!w) return;
      var re = new RegExp('(' + w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'ig');
      out = out.replace(re, '\u0001$1\u0002');
    });
    return esc(out).replace(/\u0001/g, '<mark>').replace(/\u0002/g, '</mark>');
  }
  function esc(s) { return s.replace(/[&<>]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]; }); }
  function showResults(list, words) {
    if (!results) return;
    if (list === null) { results.hidden = true; results.innerHTML = ''; if (explorer) explorer.hidden = false; return; }
    if (explorer) explorer.hidden = true;
    results.hidden = false;
    if (!list.length) { results.innerHTML = '<p class="none">Ничего не найдено.</p>'; return; }
    results.innerHTML = '<ul>' + list.map(function (d) {
      return '<li><a href="' + SITE.root + d.u + '"><i class="ty ty-' + d.k + '"></i><b>' + mark(d.t, words) + '</b>' +
        '<span>' + mark(snippet(d, words), words) + '</span></a></li>';
    }).join('') + '</ul>';
  }
  if (q) {
    q.addEventListener('focus', loadIndex);
    q.addEventListener('input', function () {
      var s = norm(q.value.trim());
      if (s.length < 2) { showResults(null); return; }
      var words = s.split(/\s+/).filter(Boolean);
      loadIndex().then(function (idx) {
        if (norm(q.value.trim()) !== s) return;
        var hits = idx.map(function (d) { return [score(d, words), d]; }).filter(function (p) { return p[0] > 0; })
          .sort(function (a, b) { return b[0] - a[0] || a[1].t.localeCompare(b[1].t); }).slice(0, 30).map(function (p) { return p[1]; });
        showResults(hits, words);
      });
    });
    q.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { var first = results && results.querySelector('a'); if (first) location.href = first.href; }
    });
  }

  /* ---------- graph ---------- */
  var COLORS = { definition: '--blue-s', theorem: '--orange-s', lemma: '--purple-s', property: '--yellow-s',
                 example: '--green-s', method: '--aqua-s', lecture: '--gray', stub: '--bg3', other: '--bg4' };
  var graphs = [];

  function neighborhood(data, id, limit) {
    var byId = {}, adj = {};
    data.nodes.forEach(function (n) { byId[n.id] = n; adj[n.id] = []; });
    data.links.forEach(function (l) { if (adj[l[0]] && adj[l[1]]) { adj[l[0]].push(l[1]); adj[l[1]].push(l[0]); } });
    var keep = {}; keep[id] = 0;
    var frontier = [id];
    for (var depth = 1; depth <= 2 && frontier.length; depth++) {
      var next = [];
      frontier.forEach(function (a) { adj[a].forEach(function (b) { if (!(b in keep)) { keep[b] = depth; next.push(b); } }); });
      frontier = next;
      if (Object.keys(keep).length > limit) break;
    }
    var ids = Object.keys(keep).sort(function (a, b) { return keep[a] - keep[b]; }).slice(0, limit);
    var set = {}; ids.forEach(function (i) { set[i] = 1; });
    return { nodes: ids.map(function (i) { var n = Object.assign({}, byId[i]); n.d = keep[i]; return n; }),
             links: data.links.filter(function (l) { return set[l[0]] && set[l[1]]; }) };
  }

  function makeGraph(canvas, data, opts) {
    var nodes = data.nodes, idx = {}, links = [];
    nodes.forEach(function (n, i) { idx[n.id] = i; n.x = (Math.random() - .5) * 200; n.y = (Math.random() - .5) * 200; n.vx = n.vy = 0; });
    data.links.forEach(function (l) { if (idx[l[0]] != null && idx[l[1]] != null) links.push([nodes[idx[l[0]]], nodes[idx[l[1]]]]); });
    var deg = {}; links.forEach(function (l) { deg[l[0].id] = (deg[l[0].id] || 0) + 1; deg[l[1].id] = (deg[l[1].id] || 0) + 1; });
    var me = opts.center != null ? nodes[idx[opts.center]] : null;
    if (me) { me.x = me.y = 0; }
    var g = canvas.getContext('2d'), W = 0, H = 0, dpr = 1;
    var view = { k: 1, x: 0, y: 0 }, hover = null, drag = null, alpha = 1, raf = null;
    var N = nodes.length, repel = opts.global ? 1400 : 2600, rest = opts.global ? 48 : 80;

    function tick() {
      for (var i = 0; i < N; i++) {
        var a = nodes[i];
        for (var j = i + 1; j < N; j++) {
          var b = nodes[j], dx = a.x - b.x, dy = a.y - b.y, d2 = dx * dx + dy * dy + 0.5, f = repel / d2;
          var d = Math.sqrt(d2); dx /= d; dy /= d;
          a.vx += dx * f; a.vy += dy * f; b.vx -= dx * f; b.vy -= dy * f;
        }
      }
      links.forEach(function (l) {
        var a = l[0], b = l[1], dx = b.x - a.x, dy = b.y - a.y, d = Math.sqrt(dx * dx + dy * dy) || 1, f = (d - rest) * 0.05;
        a.vx += dx / d * f; a.vy += dy / d * f; b.vx -= dx / d * f; b.vy -= dy / d * f;
      });
      nodes.forEach(function (n) {
        if (n === drag) { n.vx = n.vy = 0; return; }
        n.vx -= n.x * (opts.global ? 0.003 : 0.01); n.vy -= n.y * (opts.global ? 0.003 : 0.01);
        n.vx *= 0.6; n.vy *= 0.6; n.x += n.vx * alpha; n.y += n.vy * alpha;
      });
      if (me && !drag) me.x = me.y = 0;
    }
    function fit() {
      if (opts.global) return;
      var xs = nodes.map(function (n) { return n.x; }), ys = nodes.map(function (n) { return n.y; });
      var x0 = Math.min.apply(null, xs), x1 = Math.max.apply(null, xs), y0 = Math.min.apply(null, ys), y1 = Math.max.apply(null, ys);
      view.k = Math.min((W - 100) / ((x1 - x0) || 1), (H - 50) / ((y1 - y0) || 1), 2.5);
      view.x = W / 2 - (x0 + x1) / 2 * view.k; view.y = H / 2 - (y0 + y1) / 2 * view.k;
    }
    function resize() {
      dpr = devicePixelRatio || 1; W = canvas.clientWidth; H = canvas.clientHeight;
      if (!W || !H) return false;
      canvas.width = W * dpr; canvas.height = H * dpr;
      if (opts.global && !view.init) { view.k = Math.min(W, H) / 900; view.x = W / 2; view.y = H / 2; view.init = true; }
      return true;
    }
    function sx(n) { return view.x + n.x * view.k; }
    function sy(n) { return view.y + n.y * view.k; }
    function draw() {
      if (!resize()) return;
      var cs = getComputedStyle(h), c = function (v) { return cs.getPropertyValue(v).trim(); };
      g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, W, H);
      if (!opts.global) fit();
      var near = {};
      if (hover) { near[hover.id] = 1; links.forEach(function (l) { if (l[0] === hover) near[l[1].id] = 1; if (l[1] === hover) near[l[0].id] = 1; }); }
      g.lineWidth = 1;
      links.forEach(function (l) {
        var dim = hover && !(l[0] === hover || l[1] === hover);
        g.strokeStyle = c(dim ? '--bg1' : '--bg2'); g.beginPath(); g.moveTo(sx(l[0]), sy(l[0])); g.lineTo(sx(l[1]), sy(l[1])); g.stroke();
      });
      var font = c('--f-ui');
      nodes.forEach(function (n) {
        var x = sx(n), y = sy(n), r = (n === me ? 7 : 3.5 + Math.min(3, (deg[n.id] || 0) * 0.5)) * (opts.global ? Math.max(0.7, Math.min(1.4, view.k)) : 1);
        var dim = hover && !near[n.id];
        g.globalAlpha = dim ? 0.35 : 1;
        g.beginPath(); g.arc(x, y, r, 0, 6.29); g.fillStyle = c(COLORS[n.k] || '--gray'); g.fill();
        if (n === me || n === hover) { g.lineWidth = 2; g.strokeStyle = c('--fg1'); g.stroke(); g.lineWidth = 1; }
        var label = n === me || n === hover || (!opts.global && n.d <= 1 && !hover && N <= 14) || (opts.global && view.k > 1.6 && !hover) || (hover && near[n.id]);
        if (label) {
          var t = n.t.length > 28 ? n.t.slice(0, 27) + '…' : n.t;
          g.font = (n === me || n === hover ? 'bold 11px ' : '10px ') + font; g.textAlign = 'center';
          g.fillStyle = c(n.k === 'stub' ? '--fg4' : '--fg2');
          if (n === me) g.fillText(t, x, y - r - 5); else g.fillText(t, x, y + r + 11);
        }
        g.globalAlpha = 1;
      });
    }
    function loop() {
      tick(); draw();
      alpha *= 0.985;
      if (alpha > 0.02 || drag) raf = requestAnimationFrame(loop); else raf = null;
    }
    function wake(a) { alpha = Math.max(alpha, a || 0.3); if (!raf) raf = requestAnimationFrame(loop); }
    function pick(ev) {
      var rect = canvas.getBoundingClientRect(), mx = ev.clientX - rect.left, my = ev.clientY - rect.top, best = null, bd = 144;
      nodes.forEach(function (n) { var dx = sx(n) - mx, dy = sy(n) - my, d = dx * dx + dy * dy; if (d < bd) { bd = d; best = n; } });
      return best;
    }
    canvas.addEventListener('mousemove', function (ev) {
      if (drag) {
        var rect = canvas.getBoundingClientRect();
        if (drag === 'pan') { view.x = ev.clientX - rect.left - drag_off[0]; view.y = ev.clientY - rect.top - drag_off[1]; draw(); return; }
        drag.x = (ev.clientX - rect.left - view.x) / view.k; drag.y = (ev.clientY - rect.top - view.y) / view.k; wake(0.3); return;
      }
      var p = pick(ev);
      if (p !== hover) { hover = p; canvas.style.cursor = p ? 'pointer' : (opts.global ? 'grab' : 'default'); draw(); }
    });
    var drag_off = [0, 0], moved = false;
    canvas.addEventListener('mousedown', function (ev) {
      var p = pick(ev); moved = false;
      if (p) { drag = p; }
      else if (opts.global) { var rect = canvas.getBoundingClientRect(); drag = 'pan'; drag_off = [ev.clientX - rect.left - view.x, ev.clientY - rect.top - view.y]; }
      ev.preventDefault();
    });
    window.addEventListener('mousemove', function () { if (drag) moved = true; });
    window.addEventListener('mouseup', function (ev) {
      if (drag && drag !== 'pan' && !moved && drag.u) location.href = SITE.root + drag.u;
      if (drag && drag !== 'pan') wake(0.5);
      drag = null;
    });
    canvas.addEventListener('mouseleave', function () { if (hover) { hover = null; draw(); } });
    if (opts.global) {
      canvas.addEventListener('wheel', function (ev) {
        ev.preventDefault();
        var rect = canvas.getBoundingClientRect(), mx = ev.clientX - rect.left, my = ev.clientY - rect.top;
        var k = Math.exp(-ev.deltaY * 0.0015), nk = Math.max(0.15, Math.min(6, view.k * k)); k = nk / view.k;
        view.x = mx - (mx - view.x) * k; view.y = my - (my - view.y) * k; view.k = nk; draw();
      }, { passive: false });
      // touch: one finger pans, pinch zooms
      var touches = null;
      canvas.addEventListener('touchstart', function (ev) { touches = Array.prototype.map.call(ev.touches, function (t) { return [t.clientX, t.clientY]; }); }, { passive: true });
      canvas.addEventListener('touchmove', function (ev) {
        if (!touches) return;
        var now = Array.prototype.map.call(ev.touches, function (t) { return [t.clientX, t.clientY]; });
        if (now.length === 1 && touches.length === 1) { view.x += now[0][0] - touches[0][0]; view.y += now[0][1] - touches[0][1]; }
        else if (now.length === 2 && touches.length === 2) {
          var d0 = Math.hypot(touches[0][0] - touches[1][0], touches[0][1] - touches[1][1]), d1 = Math.hypot(now[0][0] - now[1][0], now[0][1] - now[1][1]);
          var k = d1 / (d0 || 1), rect = canvas.getBoundingClientRect(), mx = (now[0][0] + now[1][0]) / 2 - rect.left, my = (now[0][1] + now[1][1]) / 2 - rect.top;
          var nk = Math.max(0.15, Math.min(6, view.k * k)); k = nk / view.k;
          view.x = mx - (mx - view.x) * k; view.y = my - (my - view.y) * k; view.k = nk;
        }
        touches = now; draw(); ev.preventDefault();
      }, { passive: false });
      canvas.addEventListener('touchend', function () { touches = null; });
    }
    for (var i = 0; i < 120; i++) tick();      // прогрев, чтобы не прыгало
    graphs.push({ draw: draw });
    wake(1);
  }

  function redrawGraphs() { graphs.forEach(function (gr) { gr.draw(); }); }
  window.addEventListener('resize', redrawGraphs);

  var canvases = $$('canvas[data-graph]');
  if (canvases.length) {
    var ready = window.MOCK_GRAPH ? Promise.resolve(window.MOCK_GRAPH)
      : fetch(SITE.root + 'graph.json').then(function (r) { return r.json(); });
    ready.then(function (data) {
      canvases.forEach(function (c) {
        if (c.dataset.graph === 'global') {
          var center = new URLSearchParams(location.search).get('id');
          makeGraph(c, data, { global: true, center: center && data.nodes.some(function (n) { return n.id === center; }) ? center : null });
        } else {
          var id = SITE.id || (window.MOCK_GRAPH && window.MOCK_GRAPH.nodes[0].id);
          makeGraph(c, neighborhood(data, id, 20), { global: false, center: id });
        }
      });
      if (document.fonts) document.fonts.ready.then(redrawGraphs);
    }).catch(function () {});
  }
})();
