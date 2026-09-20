/* G20 매크로 — 차트 프리미티브 (인라인 SVG, 외부 의존성 없음)
 *
 * 목업(webui/mockups/g20-macro-mockup.html)의 차트 코드를 API 데이터 형식에 맞춰 정리한 것.
 *  - 시계열 점은 {period, value} (period: M 'YYYY-MM' · Q 'YYYY-Qn' · Y 'YYYY' · D 'YYYY-MM-DD')
 *  - 시리즈 길이가 달라도 되며 x축은 모든 시리즈의 기간 합집합(정렬)으로 만든다.
 *    값이 없거나 null인 지점은 선을 끊고(M 재시작) 툴팁·표에는 '—'로 표시한다.
 *  - 규격(제안서 5.2): 선 2px, 격자 1px 실선, 막대 ≤24px·끝 4px 라운드, 누적 구간 2px 간격,
 *    이중 축 없음, 2개 이상 시리즈면 범례, 모든 차트에 표 보기(+CSV), 값·라벨은 텍스트 색.
 *  - 툴팁은 문서 전체에서 단일 DOM(#macro-tooltip)을 쓰며 이 파일이 생성한다.
 */
(function () {
  'use strict';

  var NS = 'http://www.w3.org/2000/svg';

  // ---------- 색 (CONTRACT 11장 · 제안서 5.2 — 다크 표면 #161b22 기준 검증 통과) ----------
  var SLOT = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767'];
  var COLORS = {
    SLOT: SLOT,
    // 국가 고정색 8슬롯 — 필터로 국가가 빠져도 색이 바뀌지 않음. 나머지는 회색(OTHER).
    BY_ISO: { KR: SLOT[0], US: SLOT[1], JP: SLOT[2], CN: SLOT[3], EU: SLOT[4], GB: SLOT[5], IN: SLOT[6], BR: SLOT[7] },
    OTHER: '#8b949e',
    ACCENT: '#388bfd',
    SURFACE: '#161b22',
    INK_DARK: '#0d1117',
    GRID: '#2c2c2a',
    AXIS: '#383835',
    // 발산: 파랑(음·비둘기) ↔ 주황(양·매파), 중립 회색
    NEG: ['#1c5cab', '#3987e5', '#86b6ef'],
    NEU: '#2d333b',
    POS: ['#f0a070', '#d95926', '#a3401c']
  };

  // 국가 고정색. 슬롯 밖 국가는 회색, single=true면 단독 시리즈용으로 강조색(ACCENT)을 준다.
  function countryColor(iso, single) {
    var c = COLORS.BY_ISO[String(iso || '').toUpperCase()];
    if (c) return c;
    return single ? COLORS.ACCENT : COLORS.OTHER;
  }

  // ---------- DOM 유틸 ----------
  function svg(tag, attrs) {
    var e = document.createElementNS(NS, tag);
    if (attrs) for (var k in attrs) if (attrs.hasOwnProperty(k)) e.setAttribute(k, attrs[k]);
    return e;
  }

  function elem(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }

  function isNum(v) {
    return typeof v === 'number' && isFinite(v);
  }

  function fmt(v, d) {
    if (!isNum(v)) return '—';
    if (d === undefined || d === null) d = 1;
    return v.toLocaleString('ko-KR', { minimumFractionDigits: d, maximumFractionDigits: d });
  }

  function sgn(v, d) {
    if (!isNum(v)) return '—';
    return (v > 0 ? '+' : '') + fmt(v, d);
  }

  // 보기 좋은 눈금 (min~max를 n개 내외로 나눔)
  function niceTicks(min, max, n) {
    if (!isNum(min) || !isNum(max)) { min = 0; max = 1; }
    if (min === max) { min -= 1; max += 1; }
    n = n || 5;
    var raw = (max - min) / (n - 1);
    var mag = Math.pow(10, Math.floor(Math.log(raw) / Math.LN10));
    var norm = raw / mag;
    var step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
    var lo = Math.floor(min / step) * step, hi = Math.ceil(max / step) * step;
    var t = [];
    for (var v = lo; v <= hi + step / 2; v += step) t.push(+v.toFixed(10));
    return t;
  }

  // 상대 휘도 (밝은 배경 위 글자색 선택용)
  function lum(hex) {
    if (!hex || hex.charAt(0) !== '#' || hex.length < 7) return 0;
    var r = parseInt(hex.slice(1, 3), 16) / 255, g = parseInt(hex.slice(3, 5), 16) / 255, b = parseInt(hex.slice(5, 7), 16) / 255;
    function f(c) { return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); }
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  }

  // ---------- 툴팁 (문서 전체 단일 DOM) ----------
  var tipNode = null;

  function tipEl() {
    if (tipNode && tipNode.parentNode) return tipNode;
    tipNode = document.getElementById('macro-tooltip');
    if (!tipNode) {
      tipNode = elem('div', 'macro-tooltip');
      tipNode.id = 'macro-tooltip';
      tipNode.setAttribute('role', 'tooltip');
      tipNode.hidden = true;
      document.body.appendChild(tipNode);
    }
    return tipNode;
  }

  // rows: [{color?, label, value}]
  function showTip(ev, title, rows) {
    var tip = tipEl();
    tip.textContent = '';
    if (title) tip.appendChild(elem('div', 'tt-title', title));
    (rows || []).forEach(function (r) {
      var d = elem('div', 'tt-row');
      var i = document.createElement('i');
      i.style.background = r.color || 'transparent';
      d.appendChild(i);
      d.appendChild(elem('span', null, r.label));
      d.appendChild(elem('b', null, r.value));
      tip.appendChild(d);
    });
    tip.hidden = false;
    var cx = ev && isNum(ev.clientX) ? ev.clientX : 0, cy = ev && isNum(ev.clientY) ? ev.clientY : 0;
    var x = cx + 14, y = cy + 14, w = tip.offsetWidth, h = tip.offsetHeight;
    if (x + w > window.innerWidth - 8) x = Math.max(8, cx - w - 14);
    if (y + h > window.innerHeight - 8) y = Math.max(8, cy - h - 14);
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
  }

  function hideTip() {
    if (tipNode) tipNode.hidden = true;
  }

  // ---------- CSV ----------
  function csvCell(v) {
    if (v === null || v === undefined) return '';
    var s = String(v);
    // 수식 인젭션 방어: 문자열 셀이 = + - @ 탭 CR로 시작하면 ' 접두 (숫자 셀은 음수 그대로)
    if (typeof v === 'string' && /^[=+\-@\t\r]/.test(s)) s = "'" + s;
    if (/[",\r\n]/.test(s)) s = '"' + s.replace(/"/g, '""') + '"';
    return s;
  }

  // Blob 링크로 CSV 내려받기 (BOM 포함 — 엑셀 한글 깨짐 방지)
  function downloadCsv(name, columns, rows) {
    var lines = [columns.map(csvCell).join(',')];
    rows.forEach(function (r) { lines.push(r.map(csvCell).join(',')); });
    var blob = new Blob(['\ufeff' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = (name || 'macro') + '.csv';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  // 표 보기 토글 + CSV. rows는 표시용 문자열 배열, opt.csvRows는 원값(없으면 표시값 사용).
  // 첫 열을 제외한 열은 숫자 정렬(우측)로 취급한다 (opt.numFrom으로 시작 열 지정 가능).
  function tableView(container, columns, rows, opt) {
    opt = opt || {};
    var det = elem('details', 'table-view');
    var sum = elem('summary', null, opt.label || '표 보기 · CSV');
    det.appendChild(sum);
    var bar = elem('div', 'table-tools');
    var dl = elem('button', 'link-btn', 'CSV 내려받기');
    dl.type = 'button';
    dl.addEventListener('click', function () {
      downloadCsv(opt.csvName || 'macro', columns, opt.csvRows || rows);
    });
    bar.appendChild(dl);
    det.appendChild(bar);
    var numFrom = opt.numFrom !== undefined ? opt.numFrom : 1;
    var wrap = elem('div', 'table-wrap'), tb = elem('table', 'tbl'), thead = elem('thead'), tr = elem('tr');
    columns.forEach(function (c, i) { tr.appendChild(elem('th', i >= numFrom ? 'num' : null, c)); });
    thead.appendChild(tr);
    tb.appendChild(thead);
    var tbody = elem('tbody');
    rows.forEach(function (r) {
      var t = elem('tr');
      r.forEach(function (v, i) { t.appendChild(elem('td', i >= numFrom ? 'num' : null, v === null || v === undefined || v === '' ? '—' : v)); });
      tbody.appendChild(t);
    });
    tb.appendChild(tbody);
    wrap.appendChild(tb);
    det.appendChild(wrap);
    container.appendChild(det);
    return det;
  }

  // ---------- 기간 유틸 ----------
  // 여러 시리즈의 기간 합집합을 정렬해 반환 (같은 빈도 문자열은 사전순 = 시간순)
  function unionPeriods(series) {
    var seen = {}, out = [];
    (series || []).forEach(function (sr) {
      (sr.values || []).forEach(function (p) {
        if (!p || p.period === undefined || p.period === null) return;
        var k = String(p.period);
        if (!seen[k]) { seen[k] = true; out.push(k); }
      });
    });
    out.sort();
    return out;
  }

  // 좁은 패널에서 라벨 축약: 'YYYY-MM' → 'YY-MM', 'YYYY-Qn' → 'YY-Qn', 'YYYY-MM-DD' → 'YY-MM-DD', 'YYYY'는 그대로
  function shortLabel(label, narrow) {
    var s = String(label);
    if (!narrow || s.length <= 4) return s;
    return /^\d{4}-/.test(s) ? s.slice(2) : s;
  }

  function emptyMsg(container, msg) {
    container.appendChild(elem('p', 'empty-msg', msg || '표시할 데이터가 없습니다.'));
  }

  function legendBox(items, shape) {
    var lg = elem('div', 'legend');
    items.forEach(function (it) {
      var sp = elem('span');
      var i = document.createElement('i');
      i.className = shape || 'line';
      i.style.background = it.color;
      sp.appendChild(i);
      sp.appendChild(document.createTextNode(it.label));
      lg.appendChild(sp);
    });
    return lg;
  }

  // 막대 path (끝 4px 라운드). dir: 1 오른쪽, -1 왼쪽
  function barPath(x, yTop, w, h, dir) {
    w = Math.max(4, w);
    var r = Math.min(4, h / 2);
    if (dir < 0) {
      return 'M' + x + ',' + yTop + ' h-' + (w - r) + ' a' + r + ',' + r + ' 0 0 0 -' + r + ',' + r + ' v' + (h - 2 * r) + ' a' + r + ',' + r + ' 0 0 0 ' + r + ',' + r + ' h' + (w - r) + ' Z';
    }
    return 'M' + x + ',' + yTop + ' h' + (w - r) + ' a' + r + ',' + r + ' 0 0 1 ' + r + ',' + r + ' v' + (h - 2 * r) + ' a' + r + ',' + r + ' 0 0 1 -' + r + ',' + r + ' h-' + (w - r) + ' Z';
  }

  // ---------- 라인 차트 ----------
  // opt: { series:[{name, color, values:[{period,value}], emph}], height, yFmt, d, baseline, area,
  //        endLabel, legend, table, yMin, yMax, yTicks, onClick(period, index), csvName, unit }
  function lineChart(container, opt) {
    container.textContent = '';
    opt = opt || {};
    var series = (opt.series || []).filter(function (sr) { return sr && Array.isArray(sr.values); });
    var labels = unionPeriods(series);
    var n = labels.length;
    var idx = {};
    labels.forEach(function (l, i) { idx[l] = i; });

    // 시리즈별 합집합 인덱스 정렬 값 배열 (없으면 null)
    var grid = series.map(function (sr) {
      var arr = new Array(n);
      for (var i = 0; i < n; i++) arr[i] = null;
      sr.values.forEach(function (p) {
        if (!p) return;
        var i = idx[String(p.period)];
        if (i !== undefined && isNum(p.value)) arr[i] = p.value;
      });
      return arr;
    });

    var vals = [];
    grid.forEach(function (arr) { arr.forEach(function (v) { if (isNum(v)) vals.push(v); }); });
    if (!vals.length) { emptyMsg(container); return null; }

    var W = container.clientWidth || 600, H = opt.height || 240;
    var s = svg('svg', { width: W, height: H, 'class': 'chart', role: 'img' });
    if (opt.ariaLabel) s.setAttribute('aria-label', opt.ariaLabel);

    var yMin = Math.min.apply(null, vals), yMax = Math.max.apply(null, vals);
    if (isNum(opt.baseline)) { yMin = Math.min(yMin, opt.baseline); yMax = Math.max(yMax, opt.baseline); }
    if (isNum(opt.yMin)) yMin = Math.min(yMin, opt.yMin);
    if (isNum(opt.yMax)) yMax = Math.max(yMax, opt.yMax);
    var ticks = niceTicks(yMin, yMax, opt.yTicks || 5);
    yMin = ticks[0]; yMax = ticks[ticks.length - 1];
    var yf = opt.yFmt || function (v) { return fmt(v, opt.d !== undefined ? Math.min(opt.d, 1) : 0); };
    var vf = function (v) { return opt.yFmt ? opt.yFmt(v) : fmt(v, opt.d !== undefined ? opt.d : 2); };

    // 왼쪽 여백은 y 눈금 라벨 길이에 맞춤 (좁은 패널에서 플롯 폭 확보)
    var maxLen = 0;
    ticks.forEach(function (t) { maxLen = Math.max(maxLen, String(yf(t)).length); });
    var PAD = { top: 14, right: opt.endLabel === false ? 16 : 70, bottom: 26, left: opt.padLeft || Math.min(60, Math.max(26, Math.round(maxLen * 6.6) + 10)) };

    var pw = W - PAD.left - PAD.right, ph = H - PAD.top - PAD.bottom;
    var x = function (i) { return PAD.left + (n > 1 ? i / (n - 1) : 0.5) * pw; };
    var y = function (v) { return PAD.top + (1 - (v - yMin) / (yMax - yMin || 1)) * ph; };

    // 격자 + y 눈금
    ticks.forEach(function (t) {
      s.appendChild(svg('line', { x1: PAD.left, x2: W - PAD.right, y1: y(t), y2: y(t), 'class': 'grid' }));
      var tx = svg('text', { x: PAD.left - 6, y: y(t) + 4, 'text-anchor': 'end' });
      tx.textContent = yf(t);
      s.appendChild(tx);
    });

    // x 라벨 (좁으면 축약)
    var narrow = pw < 200;
    // 좁은 패널도 시작·끝 2개는 표시 (라벨 축약), 넓으면 95px당 1개·최대 6개
    var nx = (n < 2 || pw < 80) ? 1 : Math.max(2, Math.min(6, Math.floor(pw / 95), n));
    for (var k = 0; k < nx; k++) {
      var i = nx === 1 ? 0 : Math.round((n - 1) * k / Math.max(1, nx - 1));
      var tx2 = svg('text', { x: x(i), y: H - 8, 'text-anchor': nx === 1 ? 'middle' : k === 0 ? 'start' : k === nx - 1 ? 'end' : 'middle' });
      tx2.textContent = shortLabel(labels[i], narrow);
      s.appendChild(tx2);
    }

    if (isNum(opt.baseline)) {
      s.appendChild(svg('line', { x1: PAD.left, x2: W - PAD.right, y1: y(opt.baseline), y2: y(opt.baseline), 'class': 'base' }));
    }

    var baseY = y(isNum(opt.baseline) ? Math.max(yMin, Math.min(yMax, opt.baseline)) : yMin);
    var endLabels = []; // 끝점 라벨 겹침 방지용 {node, y}

    function drawSeries(sr, si) {
      var arr = grid[si];
      var d = '', area = '', started = false, runStart = -1, lastIdx = -1;
      for (var i = 0; i < n; i++) {
        var v = arr[i];
        if (isNum(v)) {
          d += (started ? ' L' : (d ? ' M' : 'M')) + x(i).toFixed(1) + ',' + y(v).toFixed(1);
          if (!started) runStart = i;
          started = true;
          lastIdx = i;
        } else if (started) {
          area += closeArea(runStart, i - 1);
          started = false;
        }
      }
      if (started) area += closeArea(runStart, n - 1);
      function closeArea(a, b) {
        var p = '';
        for (var j = a; j <= b; j++) p += (j === a ? 'M' : ' L') + x(j).toFixed(1) + ',' + y(arr[j]).toFixed(1);
        p += ' L' + x(b).toFixed(1) + ',' + baseY.toFixed(1) + ' L' + x(a).toFixed(1) + ',' + baseY.toFixed(1) + ' Z ';
        return p;
      }
      if (!d) return;
      var color = sr.color || COLORS.ACCENT;
      if (opt.area && sr.emph !== false) s.appendChild(svg('path', { d: area, fill: color, 'fill-opacity': 0.1 }));
      s.appendChild(svg('path', { d: d, fill: 'none', stroke: color, 'stroke-width': sr.emph === false ? 1.5 : 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round', opacity: sr.emph === false ? 0.85 : 1 }));
      // 점이 하나뿐인 구간(선이 안 그려짐)은 점으로 표시
      for (var q = 0; q < n; q++) {
        if (isNum(arr[q]) && !(q > 0 && isNum(arr[q - 1])) && !(q < n - 1 && isNum(arr[q + 1]))) {
          s.appendChild(svg('circle', { cx: x(q), cy: y(arr[q]), r: 3, fill: color }));
        }
      }
      if (sr.emph !== false && lastIdx >= 0) {
        var lx = x(lastIdx), ly = y(arr[lastIdx]);
        s.appendChild(svg('circle', { cx: lx, cy: ly, r: 6, fill: COLORS.SURFACE }));
        s.appendChild(svg('circle', { cx: lx, cy: ly, r: 4, fill: color }));
        if (opt.endLabel !== false) {
          var t = svg('text', { x: lx + 10, y: ly + 4, 'class': 'end-label' });
          t.textContent = vf(arr[lastIdx]) + (series.length > 1 ? ' ' + sr.name : '');
          s.appendChild(t);
          endLabels.push({ node: t, y: ly + 4 });
        }
      }
    }
    // 회색(비강조) 먼저, 강조 시리즈를 위에
    series.forEach(function (sr, si) { if (sr.emph === false) drawSeries(sr, si); });
    series.forEach(function (sr, si) { if (sr.emph !== false) drawSeries(sr, si); });
    // 끝점 라벨이 12px 안에 겹치면 아래로 밀어 분리 (위→아래 순서 유지)
    if (endLabels.length > 1) {
      endLabels.sort(function (a, b) { return a.y - b.y; });
      var MIN_GAP = 12;
      for (var e = 1; e < endLabels.length; e++) {
        if (endLabels[e].y - endLabels[e - 1].y < MIN_GAP) endLabels[e].y = endLabels[e - 1].y + MIN_GAP;
      }
      var overflow = endLabels[endLabels.length - 1].y - (H - PAD.bottom + 4);
      if (overflow > 0) endLabels.forEach(function (l) { l.y -= overflow; });
      endLabels.forEach(function (l) { l.node.setAttribute('y', l.y.toFixed(1)); });
    }

    // 십자선 + 전 시리즈 툴팁
    var cross = svg('line', { x1: 0, x2: 0, y1: PAD.top, y2: H - PAD.bottom, 'class': 'crosshair' });
    cross.style.display = 'none';
    s.appendChild(cross);
    var hover = svg('rect', { x: PAD.left, y: PAD.top, width: Math.max(1, pw), height: Math.max(1, ph), fill: 'transparent' });
    hover.style.cursor = opt.onClick ? 'pointer' : 'crosshair';
    function indexAt(ev) {
      var rect = s.getBoundingClientRect();
      var i = n > 1 ? Math.round((ev.clientX - rect.left - PAD.left) / pw * (n - 1)) : 0;
      return Math.max(0, Math.min(n - 1, i));
    }
    function tipAt(ev, i) {
      cross.setAttribute('x1', x(i));
      cross.setAttribute('x2', x(i));
      cross.style.display = '';
      var rows = series.map(function (sr, si) {
        var v = grid[si][i];
        return { color: sr.color || COLORS.ACCENT, label: sr.name, value: isNum(v) ? vf(v) : '—', v: isNum(v) ? v : -Infinity };
      }).sort(function (a, b) { return b.v - a.v; });
      showTip(ev, labels[i], rows);
    }
    hover.addEventListener('pointermove', function (ev) { tipAt(ev, indexAt(ev)); });
    hover.addEventListener('pointerleave', function () { cross.style.display = 'none'; hideTip(); });
    if (opt.onClick) {
      hover.addEventListener('click', function (ev) {
        var i = indexAt(ev);
        opt.onClick(labels[i], i, series.map(function (sr, si) { return grid[si][i]; }));
      });
    }
    s.appendChild(hover);
    // 키보드 포커스에서도 마지막 지점 툴팁
    s.setAttribute('tabindex', '0');
    s.addEventListener('focus', function () {
      var rect = s.getBoundingClientRect();
      tipAt({ clientX: rect.left + x(n - 1), clientY: rect.top + PAD.top }, n - 1);
    });
    s.addEventListener('blur', function () { cross.style.display = 'none'; hideTip(); });
    container.appendChild(s);

    // 단일 시리즈는 범례 생략 (제목이 시리즈를 설명)
    if (series.length > 1 && opt.legend !== false) {
      container.appendChild(legendBox(series.map(function (sr) { return { color: sr.color || COLORS.ACCENT, label: sr.name }; }), 'line'));
    }

    if (opt.table !== false) {
      var cols = ['기간'].concat(series.map(function (sr) { return sr.name; }));
      var rows = [], raw = [];
      labels.forEach(function (l, i) {
        rows.push([l].concat(series.map(function (sr, si) { var v = grid[si][i]; return isNum(v) ? fmt(v, opt.d !== undefined ? opt.d : 2) : '—'; })));
        raw.push([l].concat(series.map(function (sr, si) { var v = grid[si][i]; return isNum(v) ? v : ''; })));
      });
      tableView(container, cols, rows, { csvRows: raw, csvName: opt.csvName });
    }
    return { labels: labels, grid: grid };
  }

  // ---------- 가로 막대 ----------
  // opt: { items:[{label, value, color, extra:[{label,value}]}], max, fmt, labelW, valW, unitLabel, labelHead, table, csvName }
  function hbars(container, opt) {
    container.textContent = '';
    opt = opt || {};
    var items = (opt.items || []).filter(function (it) { return it && it.label !== undefined; });
    if (!items.length) { emptyMsg(container); return; }
    var W = container.clientWidth || 400, rowH = 26, barH = 14, labelW = opt.labelW || 110, valW = opt.valW || 64;
    var H = items.length * rowH + 6;
    var s = svg('svg', { width: W, height: H, 'class': 'chart', role: 'img' });
    var nums = items.map(function (i) { return isNum(i.value) ? Math.abs(i.value) : 0; });
    var max = isNum(opt.max) ? opt.max : Math.max.apply(null, nums);
    if (!(max > 0)) max = 1;
    var x0 = labelW, x1 = Math.max(x0 + 20, W - valW);
    var f = opt.fmt || function (v) { return fmt(v, 1); };
    s.appendChild(svg('line', { x1: x0, x2: x0, y1: 0, y2: H, 'class': 'axis' }));
    items.forEach(function (it, idx) {
      var yc = 3 + idx * rowH + rowH / 2;
      var lab = svg('text', { x: x0 - 8, y: yc + 4, 'text-anchor': 'end', 'class': 'lab' });
      lab.textContent = it.label;
      s.appendChild(lab);
      var color = it.color || COLORS.ACCENT;
      var p = null, w = 0;
      if (isNum(it.value)) {
        w = Math.max(4, Math.abs(it.value) / max * (x1 - x0));
        p = svg('path', { d: barPath(x0, yc - barH / 2, w, barH, 1), fill: color });
        s.appendChild(p);
      }
      var v = svg('text', { x: x0 + w + 8, y: yc + 4, 'class': 'val' });
      v.textContent = isNum(it.value) ? f(it.value) : '—';
      s.appendChild(v);
      var hit = svg('rect', { x: 0, y: yc - rowH / 2, width: W, height: rowH, fill: 'transparent' });
      hit.addEventListener('pointermove', function (ev) {
        if (p) p.setAttribute('opacity', 0.8);
        showTip(ev, it.label, [{ color: color, label: opt.unitLabel || '값', value: isNum(it.value) ? f(it.value) : '—' }].concat(it.extra || []));
      });
      hit.addEventListener('pointerleave', function () { if (p) p.removeAttribute('opacity'); hideTip(); });
      if (opt.onClick) { hit.style.cursor = 'pointer'; hit.addEventListener('click', function () { opt.onClick(it); }); }
      s.appendChild(hit);
    });
    container.appendChild(s);
    if (opt.table !== false) {
      tableView(container, [opt.labelHead || '항목', opt.unitLabel || '값'],
        items.map(function (i) { return [i.label, isNum(i.value) ? f(i.value) : '—']; }),
        { csvRows: items.map(function (i) { return [i.label, isNum(i.value) ? i.value : '']; }), csvName: opt.csvName });
    }
  }

  // ---------- 발산 막대 (음=파랑 왼쪽, 양=주황 오른쪽) ----------
  // opt: { items:[{label, value}], fmt, valueLabel, negLabel, posLabel, labelW, table, csvName, d }
  function divBars(container, opt) {
    container.textContent = '';
    opt = opt || {};
    var items = (opt.items || []).filter(function (it) { return it && it.label !== undefined; });
    if (!items.length) { emptyMsg(container); return; }
    var W = container.clientWidth || 400, rowH = 22, barH = 12, labelW = opt.labelW || 120, H = items.length * rowH + 6;
    var s = svg('svg', { width: W, height: H, 'class': 'chart', role: 'img' });
    var d = opt.d !== undefined ? opt.d : 1;
    var f = opt.fmt || function (v) { return sgn(v, d) + '%'; };
    var nums = items.map(function (i) { return isNum(i.value) ? Math.abs(i.value) : 0; });
    var max = Math.max.apply(null, nums) || 1;
    var cx = labelW + (W - labelW - 60) / 2, half = Math.max(20, (W - labelW - 60) / 2 - 40);
    var NEG = COLORS.NEG[1], POS = COLORS.POS[1];
    s.appendChild(svg('line', { x1: cx, x2: cx, y1: 0, y2: H, 'class': 'axis' }));
    items.forEach(function (it, idx) {
      var yc = 3 + idx * rowH + rowH / 2;
      var lab = svg('text', { x: labelW - 10, y: yc + 4, 'text-anchor': 'end', 'class': 'lab' });
      lab.textContent = it.label;
      s.appendChild(lab);
      var neg = isNum(it.value) && it.value < 0, color = neg ? NEG : POS;
      var w = 0;
      if (isNum(it.value)) {
        w = Math.max(2, Math.abs(it.value) / max * half);
        s.appendChild(svg('path', { d: barPath(cx, yc - barH / 2, w, barH, neg ? -1 : 1), fill: color }));
      }
      var v = svg('text', { x: neg ? cx - w - 6 : cx + w + 6, y: yc + 4, 'text-anchor': neg ? 'end' : 'start', 'class': 'val' });
      v.textContent = isNum(it.value) ? f(it.value) : '—';
      s.appendChild(v);
      var hit = svg('rect', { x: 0, y: yc - rowH / 2, width: W, height: rowH, fill: 'transparent' });
      hit.addEventListener('pointermove', function (ev) {
        showTip(ev, it.label, [{ color: color, label: opt.valueLabel || '변화', value: isNum(it.value) ? (opt.fmt ? f(it.value) : sgn(it.value, 2) + '%') : '—' }].concat(it.extra || []));
      });
      hit.addEventListener('pointerleave', hideTip);
      if (opt.onClick) { hit.style.cursor = 'pointer'; hit.addEventListener('click', function () { opt.onClick(it); }); }
      s.appendChild(hit);
    });
    container.appendChild(s);
    container.appendChild(legendBox([{ color: NEG, label: opt.negLabel || '음(−)' }, { color: POS, label: opt.posLabel || '양(+)' }], 'rect'));
    if (opt.table !== false) {
      tableView(container, [opt.labelHead || '항목', opt.valueLabel || '변화'],
        items.map(function (i) { return [i.label, isNum(i.value) ? f(i.value) : '—']; }),
        { csvRows: items.map(function (i) { return [i.label, isNum(i.value) ? i.value : '']; }), csvName: opt.csvName });
    }
  }

  // ---------- 누적 가로 막대 (구성 비중, 구간 사이 2px 간격) ----------
  // opt: { segments:[{label, value, color}], title, unit, d, table, csvName }
  function stackedH(container, opt) {
    container.textContent = '';
    opt = opt || {};
    var segs = (opt.segments || []).filter(function (sg) { return sg && isNum(sg.value) && sg.value > 0; });
    if (!segs.length) { emptyMsg(container); return; }
    var W = container.clientWidth || 400, barH = 20, gap = 2, H = barH + 4;
    var unit = opt.unit !== undefined ? opt.unit : '%', d = opt.d !== undefined ? opt.d : 1;
    var s = svg('svg', { width: W, height: H, 'class': 'chart', role: 'img' });
    var total = segs.reduce(function (a, b) { return a + b.value; }, 0) || 1;
    var xcur = 0;
    segs.forEach(function (sg, i) {
      var w = sg.value / total * W;
      var rw = Math.max(0, w - (i < segs.length - 1 ? gap : 0));
      var color = sg.color || COLORS.SLOT[i % 8];
      var r = svg('rect', { x: xcur, y: 2, width: rw, height: barH, fill: color, rx: i === 0 || i === segs.length - 1 ? 3 : 0 });
      s.appendChild(r);
      var label = sg.label + ' ' + fmt(sg.value, 0) + unit;
      if (label.length * 7.2 + 12 < rw) {
        var t = svg('text', { x: xcur + rw / 2, y: 2 + barH / 2 + 4, 'text-anchor': 'middle' });
        t.textContent = label;
        t.style.fill = lum(color) > 0.35 ? COLORS.INK_DARK : '#fff';
        t.style.fontSize = '11px';
        t.style.pointerEvents = 'none';
        s.appendChild(t);
      }
      r.addEventListener('pointermove', function (ev) { r.setAttribute('opacity', 0.8); showTip(ev, opt.title || '', [{ color: color, label: sg.label, value: fmt(sg.value, d) + unit }]); });
      r.addEventListener('pointerleave', function () { r.removeAttribute('opacity'); hideTip(); });
      xcur += w;
    });
    container.appendChild(s);
    container.appendChild(legendBox(segs.map(function (sg, i) { return { color: sg.color || COLORS.SLOT[i % 8], label: sg.label + ' ' + fmt(sg.value, d) + unit }; }), 'rect'));
    if (opt.table !== false) {
      tableView(container, [opt.labelHead || '항목', '비중' + (unit ? ' (' + unit + ')' : '')],
        segs.map(function (sg) { return [sg.label, fmt(sg.value, d)]; }),
        { csvRows: segs.map(function (sg) { return [sg.label, sg.value]; }), csvName: opt.csvName });
    }
  }

  // ---------- 히트맵 표 (국가 × 지표, 셀 색 = 중앙값 대비 발산) ----------
  // opt: { columns:[{key, name, unit, d, annual}], rows:[{key, label, badge, cells:{colKey:{value, period, freq, flags}}}],
  //        median:{colKey:number}, onCellClick(row, col, cell), tipRows(row, col, cell) -> [{label,value}], note, csvName, maxHeight,
  //        staleOf(row, col, cell) -> '2026-02 (지연)' | null  // 기준 기간이 오래된 셀: 우상단 회색 점 + 툴팁 첫 줄 }
  function median(a) {
    var b = a.slice().sort(function (x, y) { return x - y; });
    if (!b.length) return NaN;
    var m = b.length >> 1;
    return b.length % 2 ? b[m] : (b[m - 1] + b[m]) / 2;
  }

  // z(중앙값 대비 MAD 배수) → [배경색, 어두운 글자 여부]
  function heatColor(z) {
    if (!isNum(z)) return null;
    if (Math.abs(z) < 0.35) return [COLORS.NEU, false];
    if (z > 0) return z > 1.6 ? [COLORS.POS[2], false] : z > 0.9 ? [COLORS.POS[1], false] : [COLORS.POS[0], true];
    return z < -1.6 ? [COLORS.NEG[0], false] : z < -0.9 ? [COLORS.NEG[1], false] : [COLORS.NEG[2], true];
  }

  function heatmapTable(container, opt) {
    container.textContent = '';
    opt = opt || {};
    var cols = opt.columns || [], rows = opt.rows || [];
    if (!cols.length || !rows.length) { emptyMsg(container); return; }
    var wrap = elem('div', 'table-wrap');
    if (opt.maxHeight) wrap.style.maxHeight = opt.maxHeight + 'px';
    var table = elem('table', 'tbl heat');
    var thead = elem('thead'), tr = elem('tr');
    tr.appendChild(elem('th', 'country', opt.rowHead || '국가'));
    cols.forEach(function (c) {
      var th = elem('th', 'num', c.name);
      if (c.full && c.full !== c.name) th.title = c.full;
      var sub = (c.unit || '') + (c.annual ? (c.unit ? ' · ' : '') + '연' : '');
      if (sub) th.appendChild(elem('small', null, sub));
      tr.appendChild(th);
    });
    thead.appendChild(tr);
    table.appendChild(thead);

    // 열별 중앙값(API 제공 우선)과 MAD
    var stats = {};
    cols.forEach(function (c) {
      var a = [];
      rows.forEach(function (r) { var cell = r.cells && r.cells[c.key]; if (cell && isNum(cell.value)) a.push(cell.value); });
      var med = opt.median && isNum(opt.median[c.key]) ? opt.median[c.key] : median(a);
      var mad = median(a.map(function (v) { return Math.abs(v - med); })) * 1.4826;
      if (!(mad > 0)) mad = Math.abs(med) * 0.1 || 1;
      stats[c.key] = { med: med, mad: mad, n: a.length };
    });

    var tbody = elem('tbody'), csvRows = [], dispRows = [];
    rows.forEach(function (r) {
      var trr = elem('tr');
      var td0 = elem('td', 'country', r.label);
      if (r.badge) { var b = elem('span', 'badge badge-gray', r.badge); b.style.marginLeft = '6px'; td0.appendChild(b); }
      trr.appendChild(td0);
      var csv = [r.label], disp = [r.label];
      cols.forEach(function (c) {
        var cell = r.cells && r.cells[c.key];
        var v = cell && isNum(cell.value) ? cell.value : null;
        var td = elem('td', 'cell');
        if (v === null) {
          td.textContent = '—';
          td.classList.add('na');
          csv.push(''); disp.push('—');
        } else {
          var st = stats[c.key];
          var hc = heatColor((v - st.med) / st.mad);
          if (hc) { td.style.background = hc[0]; if (hc[1]) td.classList.add('ink-dark'); }
          td.textContent = fmt(v, c.d !== undefined ? c.d : 1);
          td.tabIndex = 0;
          csv.push(v); disp.push(fmt(v, c.d !== undefined ? c.d : 1));
          // 기준 기간이 응답 asof보다 오래된 셀: 값은 그대로 두고 우상단에 작은 회색 점만 붙인다
          var stale = opt.staleOf ? opt.staleOf(r, c, cell) : null;
          if (stale) { td.classList.add('has-stale'); td.appendChild(elem('span', 'macro-stale')); }
          (function (td, hc, st, stale) {
            function tipFn(ev) {
              var base = [];
              if (stale) base.push({ label: '기준 기간', value: String(stale) });
              base.push({ color: hc ? hc[0] : null, label: c.unit || '값', value: fmt(v, c.d !== undefined ? c.d : 1) }, { label: '중앙값', value: fmt(st.med, c.d !== undefined ? c.d : 1) });
              if (cell.period && !stale) base.push({ label: '기간', value: String(cell.period) });
              var extra = opt.tipRows ? (opt.tipRows(r, c, cell) || []) : [];
              showTip(ev, r.label + ' · ' + c.name, base.concat(extra));
            }
            td.addEventListener('pointermove', tipFn);
            td.addEventListener('pointerleave', hideTip);
            td.addEventListener('focus', function () { var rc = td.getBoundingClientRect(); tipFn({ clientX: rc.left + rc.width / 2, clientY: rc.top + rc.height / 2 }); });
            td.addEventListener('blur', hideTip);
            if (opt.onCellClick) {
              td.addEventListener('click', function () { opt.onCellClick(r, c, cell); });
              td.addEventListener('keydown', function (ev) { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); opt.onCellClick(r, c, cell); } });
            }
          })(td, hc, stats[c.key], stale);
        }
        trr.appendChild(td);
      });
      tbody.appendChild(trr);
      csvRows.push(csv); dispRows.push(disp);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    container.appendChild(wrap);

    var lg = elem('div', 'scale-legend');
    lg.appendChild(elem('span', null, '중앙값보다 낮음'));
    [COLORS.NEG[0], COLORS.NEG[1], COLORS.NEG[2], COLORS.NEU, COLORS.POS[0], COLORS.POS[1], COLORS.POS[2]].forEach(function (cc) {
      var i = document.createElement('i'); i.style.background = cc; lg.appendChild(i);
    });
    lg.appendChild(elem('span', null, '높음'));
    if (opt.note) { var nt = elem('span', 'note', opt.note); lg.appendChild(nt); }
    container.appendChild(lg);

    if (opt.table !== false) {
      var heads = [opt.rowHead || '국가'].concat(cols.map(function (c) { return c.name + (c.unit ? ' (' + c.unit + ')' : ''); }));
      tableView(container, heads, dispRows, { csvRows: csvRows, csvName: opt.csvName || 'overview' });
    }
    return stats;
  }

  // ---------- 기조 미터 (-2 비둘기 … +2 매파) ----------
  function meter(container, score) {
    container.textContent = '';
    var W = container.clientWidth || 300, H = 30;
    var s = svg('svg', { width: W, height: H, 'class': 'chart', role: 'img' });
    var pad = 6, tw = W - pad * 2, cx = pad + tw / 2;
    s.appendChild(svg('rect', { x: pad, y: 11, width: tw, height: 8, rx: 4, fill: COLORS.NEU }));
    s.appendChild(svg('line', { x1: cx, x2: cx, y1: 6, y2: 24, 'class': 'base' }));
    [-2, -1, 1, 2].forEach(function (t) {
      s.appendChild(svg('line', { x1: cx + t / 2 * tw / 2, x2: cx + t / 2 * tw / 2, y1: 21, y2: 25, 'class': 'axis' }));
    });
    if (isNum(score)) {
      var sc = Math.max(-2, Math.min(2, score));
      var w = Math.abs(sc) / 2 * tw / 2;
      var fill = sc < 0 ? COLORS.NEG[1] : COLORS.POS[1];
      if (w > 0) s.appendChild(svg('rect', { x: sc < 0 ? cx - w : cx, y: 11, width: w, height: 8, rx: 4, fill: fill }));
      var mx = cx + sc / 2 * tw / 2;
      s.appendChild(svg('circle', { cx: mx, cy: 15, r: 7, fill: COLORS.SURFACE }));
      s.appendChild(svg('circle', { cx: mx, cy: 15, r: 5, fill: sc === 0 ? COLORS.OTHER : fill }));
    }
    container.appendChild(s);
  }

  // ---------- 스파크라인 (숫자 배열 또는 {period,value} 배열) ----------
  function sparkline(values, color) {
    var nums = (values || []).map(function (p) { return p && typeof p === 'object' ? p.value : p; }).filter(isNum);
    var w = 84, h = 26, s = svg('svg', { width: w, height: h, 'class': 'spark', 'aria-hidden': 'true' });
    if (nums.length < 2) return s;
    var min = Math.min.apply(null, nums), max = Math.max.apply(null, nums);
    var x = function (i) { return 2 + i / (nums.length - 1) * (w - 10); };
    var y = function (v) { return 3 + (1 - (v - min) / (max - min || 1)) * (h - 6); };
    s.appendChild(svg('path', { d: nums.map(function (v, i) { return (i ? 'L' : 'M') + x(i).toFixed(1) + ',' + y(v).toFixed(1); }).join(' '), fill: 'none', stroke: COLORS.OTHER, 'stroke-width': 1.5 }));
    s.appendChild(svg('circle', { cx: x(nums.length - 1), cy: y(nums[nums.length - 1]), r: 3, fill: color || COLORS.ACCENT }));
    return s;
  }

  // ---------- 스탯 타일 ----------
  // sub: 값 아래 작은 보조 텍스트 (예: 원화 병기)
  function tile(label, value, delta, spark, sub) {
    var t = elem('div', 'tile');
    t.appendChild(elem('div', 'lab', label));
    t.appendChild(elem('div', 'val', value === null || value === undefined || value === '' ? '—' : value));
    if (sub) t.appendChild(elem('div', 'sub', sub));
    var d = elem('div', 'delta');
    d.appendChild(elem('span', null, delta || ''));
    if (spark) d.appendChild(spark);
    t.appendChild(d);
    return t;
  }

  window.MacroCharts = {
    lineChart: lineChart,
    hbars: hbars,
    divBars: divBars,
    stackedH: stackedH,
    heatmapTable: heatmapTable,
    meter: meter,
    sparkline: sparkline,
    tile: tile,
    tableView: tableView,
    showTip: showTip,
    hideTip: hideTip,
    fmt: fmt,
    sgn: sgn,
    niceTicks: niceTicks,
    COLORS: COLORS,
    // 보조 (macro.js에서 사용)
    countryColor: countryColor,
    unionPeriods: unionPeriods,
    downloadCsv: downloadCsv,
    isNum: isNum,
    svg: svg
  };
})();
