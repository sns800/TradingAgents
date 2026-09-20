/* G20 매크로 대시보드 뷰 (webui/macro/CONTRACT.md 9장·11장)
 *
 * app.js가 `window.MacroView.mount(rootEl, { apiFetch, elem, isAdmin })`를 1회 호출한 뒤 해시가 바뀔 때마다
 * `route(location.hash)`를 호출한다. 이 파일은 #view-macro 안쪽만 그리며 다른 화면의 DOM은 건드리지 않는다.
 *
 *  - 라우트: #/macro(개요) · #/macro/country/KR · #/macro/indicator/policy_rate · #/macro/fx · #/macro/politics/KR
 *            드로어는 쿼리 ?obs=<indicator>,<iso>,<freq>,<period> (열고 닫을 때 replaceState — hashchange 없음)
 *  - 데이터: 모두 API(/api/macro/*). 응답은 5분 메모리 캐시. 필터 변경 시 이전 차트를 흐리게 유지하고 재조회.
 *  - 차트는 charts.js(window.MacroCharts). innerHTML은 쓰지 않고 API 문자열은 항상 textContent.
 *  - 외부 링크는 http(s)/프로토콜 상대/같은 출처 경로만 <a href>로 만들고 그 외 스킴은 텍스트로만 표시한다.
 *  - 관리자(deps.isAdmin())에게는 검토 대기(pending) 문서 카드에 [승인]/[반려] 버튼을 보인다.
 */
(function () {
  'use strict';

  var C = window.MacroCharts;
  var CACHE_TTL = 5 * 60 * 1000;
  var VIEWS = ['overview', 'country', 'indicator', 'fx', 'politics'];
  var VIEW_LABEL = { overview: '개요', country: '국가 프로필', indicator: '지표 비교', fx: '통화가치', politics: '정치' };
  // 범위 → 개월 수 (전체 = 30년 미만, API 제한 30년)
  var RANGE_MONTHS = { '1y': 12, '3y': 36, '5y': 60, '10y': 120, 'all': 359 };
  var RANGE_LABEL = { '1y': '1년', '3y': '3년', '5y': '5년', '10y': '10년', 'all': '전체' };
  var FREQ_LABEL = { D: '일', W: '주', M: '월', Q: '분기', Y: '년', E: '이벤트' };
  var GROUP_LABEL = { G20: 'G20', G7: 'G7', BRICS: 'BRICS', ASIA: '아시아' };
  var UNIT_LABEL = { '%': '%', index: '지수', usd: 'USD', usd_bn: '십억 USD', lcu_per_usd: '현지통화/USD', pct_gdp: '% GDP', pct_share: '%', score: '점수', lcu_bn: '십억 자국통화' };
  var FLAG_LABEL = {
    euro_area_shared: '유로존 공통', estimated: '추정', derived: '파생', ai_generated: 'AI',
    needs_review: '검토 필요', fallback_source: '대체 소스', partial_period: '진행 중 기간'
  };
  var FLAG_BADGE = { ai_generated: 'badge-blue', needs_review: 'badge-yellow', estimated: 'badge-gray', euro_area_shared: 'badge-gray', derived: 'badge-gray', fallback_source: 'badge-yellow', partial_period: 'badge-gray' };
  var REVIEW_LABEL = { pending: ['검토 대기', 'badge-yellow'], approved: ['승인', 'badge-green'], rejected: ['반려', 'badge-red'] };
  // 링크로 허용하는 URL: http(s)://, 프로토콜 상대 //, 같은 출처 경로 /…(// 제외). javascript:/data: 등은 텍스트로만.
  var SAFE_URL_RE = /^(?:https?:\/\/|\/\/|https?:|\/(?!\/))/i;
  var INGEST_STATUS = { ok: ['정상', 'badge-green'], partial: ['일부 실패', 'badge-yellow'], failed: ['실패', 'badge-red'] };
  // 복합값(payload) 지표 — 지표 비교 select에서 제외
  var PAYLOAD_INDICATORS = { elec_mix: true, exports_top_hs2: true, top_companies: true, cb_stance: true };
  var DEFAULT_IND_COUNTRIES = ['KR', 'US', 'JP', 'CN', 'EU', 'GB'];
  // 히트맵 열 머리글 축약 (좁은 열용, 목업과 동일) — 없는 지표는 name_ko·unit 그대로
  var HEAT_SHORT = {
    policy_rate: ['정책금리', '%'], cpi_yoy: ['CPI', 'YoY %'], core_cpi_yoy: ['근원 CPI', 'YoY %'], ppi_yoy: ['PPI', 'YoY %'],
    house_price_yoy: ['집값', 'YoY %'], fx_value_index: ['통화가치', '지수'], m2_yoy: ['M2', 'YoY %'], gdp_growth: ['GDP 성장', '%'],
    mil_expenditure_share: ['국방비/지출', '%'], mil_gdp: ['국방비/GDP', '%'], party_support: ['집권당 지지', '%'], gov_approval: ['정부 지지', '%'],
    gov_debt_gdp: ['부채/GDP', '%'], exports_gdp: ['수출/GDP', '%'], energy_import_dep: ['에너지 의존', '%']
  };
  // 경쟁 정당/정부 지지율이 없는 체제 (CONTRACT 0장) — 데이터가 비었을 때 안내용
  var NO_PARTY = { CN: true, SA: true };
  var NO_GOV = { EU: true };
  // 발전원 라벨 → 색 (키워드 매칭, 없으면 슬롯 순환)
  var ENERGY_COLORS = [
    [/원자력|핵|nuclear/i, C.COLORS.SLOT[6]], [/석탄|coal/i, '#8b949e'], [/가스|gas/i, C.COLORS.SLOT[1]],
    [/재생|태양|풍력|renew|solar|wind/i, C.COLORS.SLOT[2]], [/수력|hydro/i, C.COLORS.SLOT[0]],
    [/석유|유류|oil/i, C.COLORS.SLOT[3]], [/바이오|bio/i, C.COLORS.SLOT[5]], [/기타|other/i, '#5c6370']
  ];

  // meta 조회 실패 시 최소 동작을 위한 국가 목록 (CONTRACT 1장)
  var FALLBACK_COUNTRIES = [
    ['KR', '한국', 'KRW', ['G20', 'ASIA'], false], ['US', '미국', 'USD', ['G20', 'G7'], false], ['JP', '일본', 'JPY', ['G20', 'G7', 'ASIA'], false],
    ['CN', '중국', 'CNY', ['G20', 'BRICS', 'ASIA'], false], ['EU', '유로존', 'EUR', ['G20', 'EU'], false], ['DE', '독일', 'EUR', ['G20', 'G7', 'EU'], true],
    ['FR', '프랑스', 'EUR', ['G20', 'G7', 'EU'], true], ['IT', '이탈리아', 'EUR', ['G20', 'G7', 'EU'], true], ['GB', '영국', 'GBP', ['G20', 'G7'], false],
    ['CA', '캐나다', 'CAD', ['G20', 'G7'], false], ['AU', '호주', 'AUD', ['G20', 'ASIA'], false], ['IN', '인도', 'INR', ['G20', 'BRICS', 'ASIA'], false],
    ['ID', '인도네시아', 'IDR', ['G20', 'ASIA'], false], ['BR', '브라질', 'BRL', ['G20', 'BRICS'], false], ['MX', '멕시코', 'MXN', ['G20'], false],
    ['AR', '아르헨티나', 'ARS', ['G20'], false], ['TR', '튀르키예', 'TRY', ['G20'], false], ['SA', '사우디아라비아', 'SAR', ['G20'], false],
    ['ZA', '남아프리카공화국', 'ZAR', ['G20', 'BRICS'], false], ['RU', '러시아', 'RUB', ['G20', 'BRICS'], false]
  ].map(function (r) { return { iso: r[0], name_ko: r[1], ccy: r[2], groups: r[3], euro: r[4] }; });

  // ---------- 모듈 상태 ----------
  var deps = null;      // { apiFetch, elem, isAdmin? }
  var root = null;      // #view-macro
  var ui = {};          // 주요 DOM 참조
  var mounted = false;
  var state = {
    view: 'overview', lastPath: null,
    freq: 'M', range: '5y', group: 'G20',
    country: 'KR', indicator: 'policy_rate', indCountries: DEFAULT_IND_COUNTRIES.slice(), emph: 'KR', transform: 'level',
    polCountry: 'KR',
    asof: null, drawer: null, metaError: null
  };
  var cache = {};       // path → { ts, promise }
  var meta = null;      // /macro/meta 응답
  var metaPromise = null;
  var countriesByIso = {};
  var indicatorsById = {};
  var profiles = {};    // iso → /macro/countries/{iso} 응답 (정치 표 점진 채움용)
  var resizeTimer = null;

  // ---------- 유틸 ----------
  function elem(tag, cls, text) { return deps.elem(tag, cls, text); }
  var isNum = C.isNum, fmt = C.fmt, sgn = C.sgn;

  function badge(text, cls) { return elem('span', 'badge ' + (cls || 'badge-gray'), text); }

  function note(text) { return elem('p', 'chart-note', text); }

  function errMsg(text) { return elem('p', 'empty-msg macro-error', text); }

  function flag(iso) {
    var s = String(iso || '').toUpperCase();
    if (!/^[A-Z]{2}$/.test(s)) return '';
    return String.fromCodePoint(127397 + s.charCodeAt(0), 127397 + s.charCodeAt(1));
  }

  function countryOf(iso) {
    iso = String(iso || '').toUpperCase();
    return countriesByIso[iso] || { iso: iso, name_ko: iso, ccy: '', groups: [], euro: false };
  }

  function countryName(iso) { return (flag(iso) + ' ' + countryOf(iso).name_ko).trim(); }

  function indicatorOf(id) { return indicatorsById[id] || { id: id, name_ko: id, unit: '', decimals: 1, store_freqs: [] }; }

  function indName(id) { return indicatorOf(id).name_ko || id; }

  function unitLabel(unit) { return unit ? (UNIT_LABEL[unit] || unit) : ''; }

  function decimalsOf(id, fallback) { var d = indicatorOf(id).decimals; return isNum(d) ? d : (fallback === undefined ? 1 : fallback); }

  function fmtKST(iso) {
    if (!iso) return '—';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    try {
      return new Intl.DateTimeFormat('ko-KR', { timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(d);
    } catch (e) { return d.toLocaleString('ko-KR'); }
  }

  // 짧은 KST 표기 'MM-DD HH:mm' (좁은 카드용)
  function fmtKSTShort(iso) {
    if (!iso) return '—';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    try {
      var parts = new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Seoul', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).formatToParts(d);
      var m = {};
      parts.forEach(function (p) { m[p.type] = p.value; });
      return m.month + '-' + m.day + ' ' + m.hour + ':' + m.minute;
    } catch (e) { return fmtKST(iso); }
  }

  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  function shiftMonths(d, n) { return new Date(d.getFullYear(), d.getMonth() + n, 1); }

  function periodOf(d, freq) {
    var y = d.getFullYear(), m = d.getMonth() + 1;
    if (freq === 'Y') return String(y);
    if (freq === 'Q') return y + '-Q' + Math.ceil(m / 3);
    if (freq === 'D' || freq === 'W') return y + '-' + pad2(m) + '-' + pad2(d.getDate());
    return y + '-' + pad2(m);
  }

  // 범위 시작 기간 (freq 형식). 예: 5y·M → 2021-09
  function fromPeriod(freq) { return periodOf(shiftMonths(new Date(), -RANGE_MONTHS[state.range]), freq); }
  function toPeriod(freq) { return periodOf(new Date(), freq); }
  // 범위 시작일 (YYYY-MM-DD, 월초) — fx base·index100 base용
  function fromDate() { return periodOf(shiftMonths(new Date(), -RANGE_MONTHS[state.range]), 'D'); }

  // 지표가 저장하지 않는 빈도를 골랐으면 더 낮은(거친) 빈도로 대체. 예: 연간 지표에 월 선택 → Y
  function effFreq(indId, want) {
    want = want || state.freq;
    var im = indicatorsById[indId];
    var sf = im && Array.isArray(im.store_freqs) ? im.store_freqs : null;
    if (!sf || !sf.length || sf.indexOf(want) >= 0) return want;
    var order = ['D', 'W', 'M', 'Q', 'Y'], wi = order.indexOf(want), i;
    for (i = wi + 1; i < order.length; i++) if (sf.indexOf(order[i]) >= 0) return order[i];
    for (i = wi - 1; i >= 0; i--) if (sf.indexOf(order[i]) >= 0) return order[i];
    return want;
  }

  function freqNote(indId, eff) {
    if (eff === state.freq) return null;
    return eff === 'Y'
      ? indName(indId) + '은(는) 연간만 제공 — 기간 단위(' + FREQ_LABEL[state.freq] + ') 선택과 무관하게 연간 값을 표시합니다.'
      : indName(indId) + '은(는) ' + FREQ_LABEL[eff] + ' 단위만 제공 — ' + FREQ_LABEL[eff] + ' 값을 표시합니다.';
  }

  function qs(params) {
    var out = [];
    Object.keys(params).forEach(function (k) {
      var v = params[k];
      if (v === null || v === undefined || v === '') return;
      out.push(encodeURIComponent(k) + '=' + encodeURIComponent(Array.isArray(v) ? v.join(',') : String(v)));
    });
    return out.length ? '?' + out.join('&') : '';
  }

  function seriesPath(indId, countries, freqOverride, transform) {
    var f = effFreq(indId, freqOverride);
    var p = { indicator: indId, countries: countries, freq: f, from: fromPeriod(f), to: toPeriod(f) };
    if (transform === 'index') { p.transform = 'index100'; p.base = fromPeriod(f); }
    return '/macro/series' + qs(p);
  }

  // 통화가치는 항상 G20 전체를 1회 조회(캐시 공유)하고 국가군 필터는 화면에서 적용한다.
  // 서버가 group=G7로 걸러내면 유로존(EU)이 빠져 EUR를 표시할 수 없기 때문.
  function fxPath() {
    return '/macro/fx' + qs({ base: fromDate(), freq: state.freq === 'Y' ? 'M' : state.freq, group: 'G20' });
  }

  // 5분 메모리 캐시. 실패한 요청은 캐시에서 제거해 다음 렌더에서 재시도.
  function get(path) {
    var now = Date.now(), c = cache[path];
    if (c && now - c.ts < CACHE_TTL) return c.promise;
    var p = deps.apiFetch(path).then(function (data) {
      if (data && data.error) throw new Error(String(data.error));
      return data || {};
    });
    cache[path] = { ts: now, promise: p };
    p.then(null, function () { if (cache[path] && cache[path].promise === p) delete cache[path]; });
    return p;
  }

  function ensureMeta() {
    if (meta) return Promise.resolve(meta);
    if (metaPromise) return metaPromise;
    metaPromise = get('/macro/meta').then(function (m) {
      applyMeta(m, null);
      return meta;
    }, function (err) {
      applyMeta({}, err);
      metaPromise = null; // 다음 렌더에서 재시도
      return meta;
    });
    return metaPromise;
  }

  function applyMeta(m, err) {
    meta = m || {};
    state.metaError = err ? (err.message || String(err)) : null;
    var cs = Array.isArray(meta.countries) && meta.countries.length ? meta.countries : FALLBACK_COUNTRIES;
    countriesByIso = {};
    cs.forEach(function (c) { if (c && c.iso) countriesByIso[String(c.iso).toUpperCase()] = c; });
    meta.countries = cs;
    indicatorsById = {};
    (Array.isArray(meta.indicators) ? meta.indicators : []).forEach(function (i) { if (i && i.id) indicatorsById[i.id] = i; });
    fillCountrySelects();
    fillIndicatorSelect();
    renderIngestBadge();
  }

  function groupCountries() {
    return (meta && meta.countries ? meta.countries : FALLBACK_COUNTRIES).filter(function (c) {
      return state.group === 'G20' || (Array.isArray(c.groups) && c.groups.indexOf(state.group) >= 0);
    });
  }

  // 카드 본문에 데이터 로딩: 첫 로딩은 '불러오는 중…', 재조회는 이전 내용을 흐리게 유지(스켈레톤 없음)
  function loadInto(box, promise, renderFn) {
    var token = (box.__macroReq || 0) + 1;
    box.__macroReq = token;
    if (!box.firstChild) box.appendChild(elem('p', 'empty-msg', '불러오는 중…'));
    else box.classList.add('macro-loading');
    promise.then(function (data) {
      if (box.__macroReq !== token) return;
      box.classList.remove('macro-loading');
      // 숨겨진 화면이면 그리지 않음 — 다시 보일 때 route()→render()가 캐시로 다시 그린다
      if (!box.offsetParent) return;
      box.textContent = '';
      try { renderFn(data); } catch (e) {
        box.textContent = '';
        box.appendChild(errMsg('화면 표시 중 오류: ' + (e && e.message ? e.message : e)));
        if (window.console) console.error(e);
      }
    }, function (err) {
      if (box.__macroReq !== token) return;
      box.classList.remove('macro-loading');
      box.textContent = '';
      box.appendChild(errMsg(err && err.message ? err.message : String(err)));
    });
  }

  function lastFinite(points) {
    for (var i = (points || []).length - 1; i >= 0; i--) if (points[i] && isNum(points[i].value)) return points[i];
    return null;
  }

  function prevFinite(points) {
    var seen = 0;
    for (var i = (points || []).length - 1; i >= 0; i--) if (points[i] && isNum(points[i].value)) { seen++; if (seen === 2) return points[i]; }
    return null;
  }

  function tail(points, n) {
    var f = (points || []).filter(function (p) { return p && isNum(p.value); });
    return f.slice(Math.max(0, f.length - n));
  }

  // 월 시계열을 분기/연 평균으로 (정치 지지율 등 서버가 월만 주는 시리즈용)
  function aggMean(points, freq) {
    if (freq === 'M' || !Array.isArray(points)) return points || [];
    var buckets = {}, order = [];
    points.forEach(function (p) {
      if (!p || !isNum(p.value)) return;
      var s = String(p.period), y = s.slice(0, 4), m = parseInt(s.slice(5, 7), 10) || 1;
      var key = freq === 'Q' ? y + '-Q' + Math.ceil(m / 3) : y;
      if (!buckets[key]) { buckets[key] = { sum: 0, n: 0 }; order.push(key); }
      buckets[key].sum += p.value; buckets[key].n++;
    });
    return order.map(function (k) { return { period: k, value: buckets[k].sum / buckets[k].n }; });
  }

  function toUsd(value, unit) {
    if (!isNum(value)) return null;
    return unit === 'usd_bn' ? value * 1e9 : value;
  }

  function usdAbbrev(v) {
    if (!isNum(v)) return '—';
    var a = Math.abs(v), sign = v < 0 ? '−' : '';
    if (a >= 1e12) return sign + '$' + fmt(a / 1e12, 2) + 'T';
    if (a >= 1e9) return sign + '$' + fmt(a / 1e9, 0) + 'B';
    if (a >= 1e6) return sign + '$' + fmt(a / 1e6, 0) + 'M';
    return sign + '$' + fmt(a, 0);
  }

  function krwAbbrev(won) {
    if (!isNum(won)) return '';
    var a = Math.abs(won);
    if (a >= 1e12) return fmt(a / 1e12, 0) + '조원';
    if (a >= 1e8) return fmt(a / 1e8, 0) + '억원';
    return fmt(a, 0) + '원';
  }

  // 한국이면 USD 금액에 원화 병기 (fx_usd 최신값으로 환산)
  function krwSub(usd, latest, iso) {
    if (iso !== 'KR' || !isNum(usd)) return null;
    var fx = latest && latest.fx_usd && isNum(latest.fx_usd.value) ? latest.fx_usd.value : null;
    if (!fx) return null;
    return '(약 ' + krwAbbrev(usd * fx) + ')';
  }

  function docPayload(doc) { return (doc && doc.payload) || doc || {}; }

  function flagBadges(flags) {
    var wrap = elem('span', 'flag-badges');
    (Array.isArray(flags) ? flags : []).forEach(function (f) {
      wrap.appendChild(badge(FLAG_LABEL[f] || String(f), FLAG_BADGE[f] || 'badge-gray'));
    });
    return wrap;
  }

  // 문서 배지: AI 판정/추출 + 검토 상태 + 신뢰도
  function docBadges(doc, aiLabel) {
    var wrap = elem('span', 'flag-badges');
    if (!doc) return wrap;
    if (doc.ai_generated) wrap.appendChild(badge(aiLabel || 'AI 판정', 'badge-blue'));
    var rs = REVIEW_LABEL[doc.review_status];
    if (rs) wrap.appendChild(badge(rs[0], rs[1]));
    if (isNum(doc.confidence)) wrap.appendChild(badge('신뢰도 ' + fmt(doc.confidence, 2), 'badge-gray'));
    return wrap;
  }

  function safeUrl(url) {
    var s = String(url === null || url === undefined ? '' : url).trim();
    return SAFE_URL_RE.test(s) ? s : null;
  }

  // 외부 링크. 허용 스킴이 아니면 <a> 대신 텍스트(span)로 표시한다 — API 문자열이 href로 들어가지 않게.
  function extLink(url, text) {
    var safe = safeUrl(url);
    if (!safe) return elem('span', 'unsafe-link', text || String(url === null || url === undefined ? '' : url));
    var a = elem('a', null, text || safe);
    a.href = safe;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    return a;
  }

  function isAdminUser() {
    try { return !!(deps && typeof deps.isAdmin === 'function' && deps.isAdmin()); } catch (e) { return false; }
  }

  // 검토 대기 문서의 [승인]/[반려] 버튼 (관리자만). 성공하면 카드 배지를 갱신하고 버튼을 없앤다.
  function reviewButtons(doc, head, aiLabel) {
    var wrap = elem('span', 'doc-review');
    function setDisabled(on) { Array.prototype.forEach.call(wrap.querySelectorAll('button'), function (b) { b.disabled = on; }); }
    [['approve', '승인'], ['reject', '반려']].forEach(function (a) {
      var b = elem('button', 'btn-sm doc-review-btn' + (a[0] === 'reject' ? ' btn-danger' : ''), a[1]);
      b.type = 'button';
      b.addEventListener('click', function () {
        var old = wrap.querySelector('.macro-error'); if (old) wrap.removeChild(old);
        setDisabled(true);
        deps.apiFetch('/admin/macro/review', { method: 'POST', body: JSON.stringify({ pk: doc.pk, sk: doc.sk, action: a[0] }) }).then(function (res) {
          var updated = (res && res.doc) || {};
          doc.review_status = updated.review_status || (a[0] === 'approve' ? 'approved' : 'rejected');
          if (updated.reviewed_by) doc.reviewed_by = updated.reviewed_by;
          if (updated.reviewed_at) doc.reviewed_at = updated.reviewed_at;
          var badges = head.querySelector('.flag-badges');
          if (badges) head.replaceChild(docBadges(doc, aiLabel), badges);
          if (wrap.parentNode) wrap.parentNode.removeChild(wrap);
        }, function (err) {
          setDisabled(false);
          wrap.appendChild(elem('span', 'macro-error', err && err.message ? err.message : String(err)));
        });
      });
      wrap.appendChild(b);
    });
    return wrap;
  }

  // 정성 문서 카드: 제목·배지·요약·인용 펼치기·원문 링크
  function docCard(doc, opt) {
    opt = opt || {};
    var d = elem('div', 'doc-card');
    if (!doc) { d.appendChild(elem('div', 'muted', opt.emptyText || '문서 없음')); return d; }
    var h = elem('div', 'doc-head');
    h.appendChild(elem('b', null, opt.title || doc.title_ko || '문서'));
    h.appendChild(docBadges(doc, opt.aiLabel));
    if (doc.review_status === 'pending' && doc.pk && doc.sk && isAdminUser()) h.appendChild(reviewButtons(doc, h, opt.aiLabel));
    d.appendChild(h);
    var metaLine = [];
    if (doc.date) metaLine.push(doc.date);
    if (doc.source_name) metaLine.push(doc.source_name);
    if (metaLine.length) d.appendChild(elem('div', 'doc-meta', metaLine.join(' · ')));
    if (doc.summary_ko) d.appendChild(elem('div', 'doc-summary', doc.summary_ko));
    if (opt.extra) d.appendChild(opt.extra);
    var quotes = Array.isArray(doc.quotes) ? doc.quotes.filter(Boolean) : [];
    if (quotes.length) {
      var det = elem('details', 'quotes');
      det.appendChild(elem('summary', null, '인용 ' + quotes.length + '건 펼치기'));
      quotes.forEach(function (q) { det.appendChild(elem('div', 'q', '“' + q + '”')); });
      d.appendChild(det);
    }
    if (doc.source_url) { var l = elem('div', 'doc-link'); l.appendChild(extLink(doc.source_url, '원문 보기 →')); d.appendChild(l); }
    return d;
  }

  function replaceHash(hash) {
    try { history.replaceState(null, '', location.pathname + location.search + hash); } catch (e) { location.hash = hash; }
  }

  function viewHash(view) {
    if (view === 'country') return '#/macro/country/' + state.country;
    if (view === 'indicator') return '#/macro/indicator/' + state.indicator;
    if (view === 'fx') return '#/macro/fx';
    if (view === 'politics') return '#/macro/politics/' + state.polCountry;
    return '#/macro';
  }

  function currentPathHash() { return viewHash(state.view); }

  // ---------- 마운트: 뼈대 DOM ----------
  function seg(name, options, current, onChange) {
    var s = elem('span', 'seg');
    s.dataset.filter = name;
    options.forEach(function (o) {
      var b = elem('button', o[0] === current ? 'active' : null, o[1]);
      b.type = 'button';
      b.dataset.v = o[0];
      b.addEventListener('click', function () {
        if (state[name] === o[0]) return;
        state[name] = o[0];
        Array.prototype.forEach.call(s.querySelectorAll('button'), function (x) { x.classList.toggle('active', x.dataset.v === o[0]); });
        onChange(o[0]);
      });
      s.appendChild(b);
    });
    return s;
  }

  function card(title, sub, rightNodes) {
    var c = elem('div', 'card macro-card');
    var h = elem('h3', null, title);
    if (sub) h.appendChild(elem('span', 'sub', sub));
    var right = elem('span', 'right');
    (rightNodes || []).forEach(function (n) { right.appendChild(n); });
    h.appendChild(right);
    c.appendChild(h);
    c.__head = h;
    c.__right = right;
    return c;
  }

  function buildSkeleton() {
    root.textContent = '';
    root.classList.add('macro-root');

    // 헤더: 제목 + 서브 내비 + 수집 배지
    var head = elem('div', 'macro-head');
    head.appendChild(elem('h2', null, 'G20 매크로'));
    var nav = elem('nav', 'sub-nav');
    nav.setAttribute('aria-label', 'G20 매크로 화면');
    VIEWS.forEach(function (v) {
      var b = elem('button', null, VIEW_LABEL[v]);
      b.type = 'button';
      b.dataset.view = v;
      b.addEventListener('click', function () { location.hash = viewHash(v); });
      nav.appendChild(b);
    });
    head.appendChild(nav);
    ui.ingestBadge = badge('수집 현황 확인 중…', 'badge-gray');
    head.appendChild(ui.ingestBadge);
    root.appendChild(head);

    // 공통 필터 행
    var fr = elem('div', 'filter-row');
    function filt(label, node) { var f = elem('div', 'filter', label + ' '); f.appendChild(node); return f; }
    fr.appendChild(filt('기간 단위', seg('freq', [['M', '월'], ['Q', '분기'], ['Y', '년']], state.freq, render)));
    fr.appendChild(filt('범위', seg('range', [['1y', '1년'], ['3y', '3년'], ['5y', '5년'], ['10y', '10년'], ['all', '전체']], state.range, render)));
    fr.appendChild(filt('국가군', seg('group', [['G20', 'G20'], ['G7', 'G7'], ['BRICS', 'BRICS'], ['ASIA', '아시아']], state.group, render)));
    ui.asof = elem('span', 'asof', '');
    fr.appendChild(ui.asof);
    root.appendChild(fr);

    ui.sections = {};
    buildOverview();
    buildCountry();
    buildIndicator();
    buildFx();
    buildPolitics();
    buildDrawer();

    root.appendChild(elem('p', 'footer-src', '출처: BIS · OECD · World Bank · IMF · FRED · Ember(CC BY 4.0) · Our World in Data · WITS · 각국 중앙은행 · 위키피디아 여론조사 문서. AI 판정/추출 값은 배지로 표시되며 검토 상태를 함께 보여줍니다.'));
  }

  function section(view) {
    var s = elem('section', 'macro-view');
    s.id = 'macro-view-' + view;
    s.hidden = true;
    ui.sections[view] = s;
    root.appendChild(s);
    return s;
  }

  // ① 개요
  function buildOverview() {
    var s = section('overview');
    var grid = elem('div', 'overview-grid');
    var left = elem('div');
    var heat = card('국가 × 지표 히트맵', '최신값 · 셀 색 = 해당 지표의 국가군 중앙값 대비 위치', [badge('셀 클릭 → 관측치 상세', 'badge-gray')]);
    ui.ovHeat = elem('div', 'card-body');
    heat.appendChild(ui.ovHeat);
    left.appendChild(heat);
    grid.appendChild(left);
    var right = elem('div');
    var ing = card('수집 현황', '소스별 마지막 수집 · 다음 예정');
    ui.ovIngest = elem('div', 'card-body');
    ing.appendChild(ui.ovIngest);
    right.appendChild(ing);
    var brief = card('주간 G20 브리프', 'AI 생성 · 근거 문서 링크');
    ui.ovBrief = elem('div', 'card-body');
    brief.appendChild(ui.ovBrief);
    right.appendChild(brief);
    grid.appendChild(right);
    s.appendChild(grid);
  }

  // ② 국가 프로필
  function buildCountry() {
    var s = section('country');
    var ph = elem('div', 'profile-head');
    ui.pfFlag = elem('span', 'flag', '');
    ph.appendChild(ui.pfFlag);
    var nm = elem('div');
    ui.pfName = elem('div', 'name', '');
    ui.pfMeta = elem('div', 'meta');
    nm.appendChild(ui.pfName);
    nm.appendChild(ui.pfMeta);
    ph.appendChild(nm);
    ui.pfSelect = elem('select', 'country-select');
    ui.pfSelect.setAttribute('aria-label', '국가 선택');
    ui.pfSelect.addEventListener('change', function () { state.country = ui.pfSelect.value; location.hash = viewHash('country'); });
    ph.appendChild(ui.pfSelect);
    s.appendChild(ph);

    var grid = elem('div', 'grid2');
    // 정치
    var pol = card('정치', '정당 지지율 · 여론조사 평균');
    ui.pfPolBadge = elem('span');
    pol.__right.appendChild(ui.pfPolBadge);
    ui.pfPolitics = elem('div', 'card-body');
    pol.appendChild(ui.pfPolitics);
    grid.appendChild(pol);
    // 통화정책
    var mon = card('통화정책', '정책금리 · 중앙은행 기조');
    ui.pfMonBadge = elem('span');
    mon.__right.appendChild(ui.pfMonBadge);
    ui.pfRate = elem('div', 'card-body chart-box');
    mon.appendChild(ui.pfRate);
    ui.pfStance = elem('div', 'card-body');
    mon.appendChild(ui.pfStance);
    ui.pfMoney = elem('div', 'card-body tiles tiles-2');
    mon.appendChild(ui.pfMoney);
    grid.appendChild(mon);
    // 재정·규모
    var fis = card('재정·규모', '연간 · USD 기준');
    ui.pfFiscal = elem('div', 'card-body tiles');
    fis.appendChild(ui.pfFiscal);
    ui.pfMil = elem('div', 'card-body chart-box');
    fis.appendChild(ui.pfMil);
    grid.appendChild(fis);
    // 경제구조
    var st = card('경제구조', '산업 비중 · 상위 수출 · 시총 상위 기업');
    ui.pfStructure = elem('div', 'card-body');
    st.appendChild(ui.pfStructure);
    grid.appendChild(st);
    // 에너지
    var en = card('에너지', '발전 믹스 · 연료별 해외의존도');
    ui.pfEnergy = elem('div', 'card-body');
    en.appendChild(ui.pfEnergy);
    grid.appendChild(en);
    // 시장경제
    var mk = card('시장경제', '전년 동기 대비 % · 같은 축 척도 · 0 기준선');
    ui.pfMarket = elem('div', 'card-body');
    mk.appendChild(ui.pfMarket);
    grid.appendChild(mk);
    s.appendChild(grid);
  }

  // ③ 지표 비교
  function buildIndicator() {
    var s = section('indicator');
    var c = elem('div', 'card macro-card');
    var bar = elem('div', 'indicator-bar');
    var w1 = elem('span', null, '지표 ');
    ui.indSelect = elem('select');
    ui.indSelect.setAttribute('aria-label', '지표 선택');
    ui.indSelect.addEventListener('change', function () { state.indicator = ui.indSelect.value; location.hash = viewHash('indicator'); });
    w1.appendChild(ui.indSelect);
    bar.appendChild(w1);
    var w2 = elem('span', null, '국가 ');
    ui.indChips = elem('span', 'chips');
    w2.appendChild(ui.indChips);
    bar.appendChild(w2);
    var w3 = elem('span', null, '강조 ');
    ui.indEmph = elem('select');
    ui.indEmph.setAttribute('aria-label', '강조 국가');
    ui.indEmph.addEventListener('change', function () { state.emph = ui.indEmph.value; render(); });
    w3.appendChild(ui.indEmph);
    bar.appendChild(w3);
    var w4 = elem('span', null, '변환 ');
    w4.appendChild(seg('transform', [['level', '수준'], ['index', '지수화 (범위 시작=100)']], state.transform, render));
    bar.appendChild(w4);
    c.appendChild(bar);
    ui.indTitle = elem('h3', null, '');
    c.appendChild(ui.indTitle);
    ui.indNote = elem('div');
    c.appendChild(ui.indNote);
    ui.indChart = elem('div', 'card-body chart-box');
    c.appendChild(ui.indChart);
    c.appendChild(note('강조 모드: 선택 국가만 고정 색, 나머지는 회색. 8개국을 넘기면 소형 다중 패널로 자동 전환. 이중 축 없음 — 규모가 다른 지표는 지수화로 비교.'));
    s.appendChild(c);
  }

  // ④ 통화가치
  function buildFx() {
    var s = section('fx');
    var c = card('달러 대비 통화가치 지수', '기준 = 범위 시작 시점 100 · 100 아래 = 달러 대비 약세', [badge('지수 = 기준일 환율 ÷ 현재 환율 × 100 (환율 = 현지통화/USD)', 'badge-gray')]);
    ui.fxBase = elem('span');
    c.__right.appendChild(ui.fxBase);
    ui.fxGrid = elem('div', 'card-body');
    c.appendChild(ui.fxGrid);
    s.appendChild(c);
    var g = elem('div', 'grid2');
    var c2 = card('기간 변화율', '통화가치, 범위 시작 대비 %');
    ui.fxBars = elem('div', 'card-body chart-box');
    c2.appendChild(ui.fxBars);
    g.appendChild(c2);
    var c3 = card('달러지수 (DXY)', '별도 차트 — 이중 축 대신');
    ui.fxDxy = elem('div', 'card-body chart-box');
    c3.appendChild(ui.fxDxy);
    g.appendChild(c3);
    s.appendChild(g);
  }

  // ⑤ 정치
  function buildPolitics() {
    var s = section('politics');
    var c = card('G20 정당 지지율·집권당', '여론조사 평균(최근 30일) · 국가 간 직접 비교 불가', [badge('AI 추출 + 규칙 검증', 'badge-blue')]);
    ui.polTable = elem('div', 'card-body');
    c.appendChild(ui.polTable);
    c.appendChild(note('중국·사우디아라비아: 경쟁 정당 없음. 집권당·다음 선거·신뢰도 열은 국가를 선택하면 채워집니다(국가별 프로필 호출을 20회 반복하지 않기 위한 점진 로딩). 신뢰도는 최근 30일 조사 건수 기준(5건 이상 높음 · 2건 이상 보통 · 그 외 낮음).'));
    s.appendChild(c);
    var c2 = card('정당 지지율', '월 평균 · 최대 4당, 나머지 \'기타\'');
    ui.polDetailTitle = c2.__head.firstChild;
    ui.polDetailBadge = elem('span');
    c2.__right.appendChild(ui.polDetailBadge);
    ui.polDetail = elem('div', 'card-body');
    c2.appendChild(ui.polDetail);
    s.appendChild(c2);
  }

  // 관측치 상세 드로어
  function buildDrawer() {
    var d = elem('aside', 'macro-drawer');
    d.hidden = true;
    d.setAttribute('role', 'dialog');
    d.setAttribute('aria-label', '관측치 상세');
    var close = elem('button', 'close', '닫기');
    close.type = 'button';
    close.addEventListener('click', function () { closeDrawer(false); });
    d.appendChild(close);
    d.appendChild(elem('div', 'sect', '관측치 상세'));
    ui.drBody = elem('div');
    d.appendChild(ui.drBody);
    root.appendChild(d);
    ui.drawer = d;
  }

  function fillCountrySelects() {
    if (!ui.pfSelect) return;
    var cs = meta && meta.countries ? meta.countries : FALLBACK_COUNTRIES;
    ui.pfSelect.textContent = '';
    cs.forEach(function (c) {
      var o = elem('option', null, countryName(c.iso));
      o.value = c.iso;
      ui.pfSelect.appendChild(o);
    });
    ui.pfSelect.value = state.country;
    // 지표 비교 국가 칩
    ui.indChips.textContent = '';
    cs.forEach(function (c) {
      var b = elem('button', 'chip');
      b.type = 'button';
      var i = document.createElement('i');
      i.style.background = C.countryColor(c.iso);
      b.appendChild(i);
      b.appendChild(document.createTextNode(flag(c.iso) + ' ' + c.iso));
      b.title = c.name_ko || c.iso;
      b.dataset.iso = c.iso;
      b.addEventListener('click', function () {
        var k = state.indCountries.indexOf(c.iso);
        if (k >= 0) state.indCountries.splice(k, 1); else state.indCountries.push(c.iso);
        render();
      });
      ui.indChips.appendChild(b);
    });
  }

  function fillIndicatorSelect() {
    if (!ui.indSelect) return;
    var list = (meta && Array.isArray(meta.indicators) ? meta.indicators : []).filter(function (i) {
      return i && i.id && i.category !== 'politics' && i.category !== 'composite' && !PAYLOAD_INDICATORS[i.id];
    });
    ui.indSelect.textContent = '';
    if (!list.length) list = [{ id: state.indicator, name_ko: indName(state.indicator), unit: '' }];
    if (!list.some(function (i) { return i.id === state.indicator; })) list.unshift({ id: state.indicator, name_ko: indName(state.indicator), unit: '' });
    list.forEach(function (i) {
      var o = elem('option', null, (i.name_ko || i.id) + (i.unit ? ' (' + unitLabel(i.unit) + ')' : ''));
      o.value = i.id;
      ui.indSelect.appendChild(o);
    });
    ui.indSelect.value = state.indicator;
  }

  function renderIngestBadge() {
    var b = ui.ingestBadge;
    if (!b) return;
    if (state.metaError) {
      b.className = 'badge badge-red';
      b.textContent = '메타 조회 실패: ' + state.metaError;
      return;
    }
    var ing = meta && meta.ingest ? meta.ingest : {};
    var keys = Object.keys(ing), ok = 0, last = null;
    keys.forEach(function (k) {
      var r = ing[k] || {};
      if (r.status === 'ok') ok++;
      if (r.finished_at && (!last || r.finished_at > last)) last = r.finished_at;
    });
    if (!keys.length) { b.className = 'badge badge-gray'; b.textContent = '수집 기록 없음'; return; }
    b.className = 'badge ' + (ok === keys.length ? 'badge-gray' : 'badge-yellow');
    b.textContent = '마지막 수집 ' + fmtKST(last) + ' KST · 소스 ' + ok + '/' + keys.length + ' 정상';
  }

  function renderAsof() {
    var a = state.asof;
    if (!a) { ui.asof.textContent = ''; return; }
    var parts = [];
    if (a.M) parts.push(a.M + ' (월)');
    if (a.Q) parts.push(a.Q + ' (분기)');
    if (a.Y) parts.push(a.Y + ' (년)');
    ui.asof.textContent = parts.length ? '기준 ' + parts.join(' · ') : '';
  }

  // ---------- 라우팅 ----------
  function parseRoute(hash) {
    var h = hash || '#/macro', q = '';
    var qi = h.indexOf('?');
    if (qi >= 0) { q = h.slice(qi + 1); h = h.slice(0, qi); }
    var m = h.match(/^#\/macro(?:\/([a-z]+)(?:\/([A-Za-z0-9_\-]+))?)?\/?$/);
    var view = 'overview', param = null;
    if (m && m[1]) { view = m[1]; param = m[2] ? decodeURIComponent(m[2]) : null; }
    if (VIEWS.indexOf(view) < 0) view = 'overview';
    var obs = null;
    var om = q.match(/(?:^|&)obs=([^&]+)/);
    if (om) {
      var parts = decodeURIComponent(om[1]).split(',');
      if (parts.length === 4) obs = { indicator: parts[0], iso: parts[1].toUpperCase(), freq: parts[2], period: parts[3] };
    }
    return { view: view, param: param, obs: obs, path: h };
  }

  function showView(view) {
    VIEWS.forEach(function (v) { ui.sections[v].hidden = v !== view; });
    Array.prototype.forEach.call(root.querySelectorAll('.sub-nav button'), function (b) { b.classList.toggle('active', b.dataset.view === view); });
  }

  function route(hash) {
    if (!mounted) return;
    var r = parseRoute(hash || location.hash);
    if (r.view === 'country' && r.param) state.country = r.param.toUpperCase();
    if (r.view === 'indicator' && r.param) state.indicator = r.param;
    if (r.view === 'politics' && r.param) state.polCountry = r.param.toUpperCase();
    state.view = r.view;
    state.lastPath = r.path;
    showView(r.view);
    C.hideTip();
    render();
    if (r.obs) openDrawer(r.obs.indicator, r.obs.iso, r.obs.freq, r.obs.period, true);
    else closeDrawer(true);
  }

  function render() {
    if (!mounted) return;
    C.hideTip();
    ensureMeta().then(function () {
      if (!mounted) return;
      if (ui.pfSelect && ui.pfSelect.value !== state.country) ui.pfSelect.value = state.country;
      if (ui.indSelect && ui.indSelect.value !== state.indicator) { fillIndicatorSelect(); }
      if (state.view === 'overview') renderOverview();
      else if (state.view === 'country') renderCountry();
      else if (state.view === 'indicator') renderIndicator();
      else if (state.view === 'fx') renderFx();
      else renderPolitics();
    });
  }

  // ---------- ① 개요 ----------
  // 지연 판정: 셀 기준 기간이 응답 asof보다 M 3개월 · Q 2분기 · Y 2년 이상 오래되면 '지연'
  var STALE_GAP = { M: 3, Q: 2, Y: 2 };

  function periodIndex(freq, period) {
    var p = String(period || ''), y = parseInt(p.slice(0, 4), 10);
    if (!isNum(y)) return null;
    if (freq === 'Y') return y;
    if (freq === 'Q') { var q = parseInt(p.slice(6), 10); return isNum(q) ? y * 4 + (q - 1) : null; }
    var m = parseInt(p.slice(5, 7), 10);
    return isNum(m) ? y * 12 + (m - 1) : null;
  }

  // 반환: '2026-02 (지연)' 또는 null. 연간 지표(gdp_growth·mil_expenditure_share 등)는 Y 기준.
  function staleNote(col, cell) {
    if (!cell || !cell.period) return null;
    var freq = col && col.annual ? 'Y' : (cell.freq || state.freq);
    if (!STALE_GAP[freq]) return null;
    var a = state.asof && state.asof[freq];
    if (!a) return null;
    var now = periodIndex(freq, a), got = periodIndex(freq, cell.period);
    if (now === null || got === null) return null;
    return now - got >= STALE_GAP[freq] ? String(cell.period) + ' (지연)' : null;
  }

  function heatColumns(data) {
    var list = Array.isArray(data.indicators) ? data.indicators : [];
    return list.map(function (it) {
      var im = typeof it === 'string' ? indicatorOf(it) : (indicatorsById[it.id] ? Object.assign({}, indicatorsById[it.id], it) : it);
      var id = typeof it === 'string' ? it : it.id;
      var d = isNum(im.decimals) ? im.decimals : decimalsOf(id, 1);
      var sh = HEAT_SHORT[id];
      return { key: id, name: sh ? sh[0] : (im.name_ko || id), unit: sh ? sh[1] : unitLabel(im.unit), full: im.name_ko || id, d: Math.min(d, 2), annual: im.native_freq === 'Y' || (Array.isArray(im.store_freqs) && im.store_freqs.length === 1 && im.store_freqs[0] === 'Y'), meta: im };
    });
  }

  function renderOverview() {
    var p = get('/macro/overview' + qs({ group: state.group }));
    loadInto(ui.ovHeat, p, function (data) {
      state.asof = data.asof || null;
      renderAsof();
      var cols = heatColumns(data);
      var rows = (Array.isArray(data.rows) ? data.rows : []).map(function (r) {
        var c = countryOf(r.iso);
        return { key: r.iso, iso: r.iso, label: countryName(r.iso), badge: c.euro ? '유로존' : null, cells: r.cells || {} };
      });
      if (!cols.length || !rows.length) { ui.ovHeat.appendChild(elem('p', 'empty-msg', '표시할 데이터가 없습니다.')); return; }
      C.heatmapTable(ui.ovHeat, {
        columns: cols, rows: rows, median: data.median || {}, maxHeight: 760, csvName: 'g20-overview-' + state.group,
        note: '색은 ' + GROUP_LABEL[state.group] + ' 중앙값 대비 순위 위치이며 좋음/나쁨 판단이 아님 · 연간 지표(연 표기)는 기간 단위와 무관하게 최신 연도 · 셀 우상단 회색 점 = 기준 기간 지연',
        staleOf: function (row, col, cell) { return staleNote(col, cell); },
        tipRows: function (row, col, cell) {
          var extra = [];
          if (isNum(cell.change)) extra.push({ label: '전기 대비', value: sgn(cell.change, col.d) });
          // 순위 rank/n. 유로 복제 셀은 EU 값이라 국가 순위가 아니다
          if (Array.isArray(cell.flags) && cell.flags.indexOf('euro_area_shared') >= 0) extra.push({ label: '순위', value: '—(유로존 공통)' });
          else if (isNum(cell.rank)) extra.push({ label: '순위', value: cell.rank + (isNum(cell.n) && cell.n > 0 ? '/' + cell.n : '') });
          if (Array.isArray(cell.flags) && cell.flags.length) extra.push({ label: '플래그', value: cell.flags.map(function (f) { return FLAG_LABEL[f] || f; }).join(', ') });
          return extra;
        },
        onCellClick: function (row, col, cell) {
          if (!cell || !cell.period) return;
          openDrawer(col.key, row.iso, cell.freq || (col.annual ? 'Y' : state.freq), cell.period, false);
        }
      });
    });

    loadInto(ui.ovIngest, ensureMeta(), function (m) {
      renderIngestBadge();
      var ing = m && m.ingest ? m.ingest : {};
      var keys = Object.keys(ing);
      if (state.metaError) { ui.ovIngest.appendChild(errMsg('수집 현황을 불러오지 못했습니다: ' + state.metaError)); return; }
      if (!keys.length) { ui.ovIngest.appendChild(elem('p', 'empty-msg', '수집 기록이 아직 없습니다.')); return; }
      // 좁은 우측 열이라 표 대신 소스별 2줄 목록: [소스 · 상태] / [마지막 수집 · 다음 예정(지연 배지)]
      var now = Date.now();
      keys.sort().forEach(function (k) {
        var r = ing[k] || {}, row = elem('div', 'ingest-row'), top = elem('div', 'ingest-top');
        top.appendChild(elem('span', 'src', k));
        var st = INGEST_STATUS[r.status] || [r.status || '—', 'badge-gray'];
        top.appendChild(badge(st[0], st[1]));
        row.appendChild(top);
        var sub = elem('div', 'ingest-sub');
        var fin = elem('span', null, '마지막 ' + (r.finished_at ? fmtKSTShort(r.finished_at) : '—'));
        fin.title = (r.finished_at ? fmtKST(r.finished_at) + ' KST' : '') + (isNum(r.n_obs) ? ' · 관측치 ' + fmt(r.n_obs, 0) + '건' : '');
        sub.appendChild(fin);
        if (r.next_due) {
          var due = new Date(r.next_due).getTime();
          var late = isNum(due) && due < now;
          var nx = elem('span', null, '· 다음 ' + fmtKSTShort(r.next_due));
          nx.title = fmtKST(r.next_due) + ' KST';
          sub.appendChild(nx);
          if (late) sub.appendChild(badge('지연', 'badge-yellow'));
        }
        row.appendChild(sub);
        ui.ovIngest.appendChild(row);
      });
    });

    loadInto(ui.ovBrief, get('/macro/docs' + qs({ type: 'weekly_brief', iso: 'G20', limit: 1 })), function (data) {
      var doc = Array.isArray(data.docs) && data.docs.length ? data.docs[0] : null;
      if (!doc) { ui.ovBrief.appendChild(elem('p', 'empty-msg', '아직 생성되지 않음')); return; }
      var pl = docPayload(doc);
      var extra = elem('div');
      if (Array.isArray(pl.bullets) && pl.bullets.length) {
        var ul = elem('ul', 'brief-list');
        pl.bullets.forEach(function (b) { ul.appendChild(elem('li', null, String(b))); });
        extra.appendChild(ul);
      }
      if (Array.isArray(pl.evidence) && pl.evidence.length) {
        var det = elem('details', 'quotes');
        det.appendChild(elem('summary', null, '근거 문서 ' + pl.evidence.length + '건'));
        pl.evidence.forEach(function (e) {
          var li = elem('div', 'q');
          if (e && e.url) li.appendChild(extLink(e.url, e.doc_key || e.url)); else li.textContent = e && e.doc_key ? e.doc_key : String(e);
          det.appendChild(li);
        });
        extra.appendChild(det);
      }
      ui.ovBrief.appendChild(docCard(doc, { title: doc.title_ko || ('주간 브리프' + (pl.week_start ? ' · ' + pl.week_start + ' 주' : '')), aiLabel: 'AI 생성', extra: extra }));
    });
  }

  // ---------- ② 국가 프로필 ----------
  function renderCountry() {
    var iso = state.country, c = countryOf(iso);
    ui.pfFlag.textContent = flag(iso);
    ui.pfName.textContent = c.name_ko;
    if (ui.pfSelect.value !== iso) ui.pfSelect.value = iso;
    var profile = get('/macro/countries/' + encodeURIComponent(iso));
    profile.then(function (d) { profiles[iso] = d; }, function () {});

    // 헤더 메타 (집권당·정부 지지율·다음 선거·통화)
    loadInto(ui.pfMeta, profile, function (d) {
      var docs = d.docs || {}, el = docPayload(docs.election), pp = docPayload(docs.poll_of_polls);
      var items = [];
      var ruling = el.ruling_party || pp.ruling_party;
      items.push(['집권', ruling ? ruling + (el.ruling_lean ? ' · ' + el.ruling_lean : '') : (NO_PARTY[iso] ? '경쟁 정당 없음' : '—')]);
      items.push(['정부 지지율', isNum(pp.gov_approval) ? fmt(pp.gov_approval, 0) + '%' : (NO_GOV[iso] || NO_PARTY[iso] ? '해당 없음' : '—')]);
      items.push(['다음 선거', el.next_election_date ? el.next_election_date + (el.election_type ? ' ' + el.election_type : '') : '—']);
      var cc = d.country || c;
      items.push(['통화', (cc.ccy || c.ccy || '—') + (cc.euro || c.euro ? ' (유로존 공통 정책)' : '')]);
      items.forEach(function (m) { var s = elem('span'); s.appendChild(elem('b', null, m[0] + ' ')); s.appendChild(document.createTextNode(m[1])); ui.pfMeta.appendChild(s); });
    });

    // 정치 카드
    var polPath = '/macro/politics/' + encodeURIComponent(iso) + qs({ from: fromPeriod('M') });
    loadInto(ui.pfPolitics, get(polPath), function (pol) { renderPartyBlock(ui.pfPolitics, ui.pfPolBadge, iso, pol, 200); });

    // 통화정책: 정책금리 라인 (+미국 참고)
    var rateCountries = iso === 'US' ? ['US'] : [iso, 'US'];
    loadInto(ui.pfRate, get(seriesPath('policy_rate', rateCountries)), function (d) {
      ui.pfMonBadge.textContent = '';
      var shared = Array.isArray(d.euro_shared) && d.euro_shared.indexOf(iso) >= 0;
      if (shared || c.euro) ui.pfMonBadge.appendChild(badge('유로존 공통', 'badge-gray'));
      var series = [{ name: c.name_ko + (shared ? ' (유로존)' : ''), color: C.countryColor(iso, true), values: (d.series && d.series[iso]) || [] }];
      if (iso !== 'US') series.push({ name: '미국 (참고)', color: C.COLORS.OTHER, values: (d.series && d.series.US) || [], emph: false });
      var fn = freqNote('policy_rate', effFreq('policy_rate'));
      if (fn) ui.pfRate.appendChild(note(fn));
      C.lineChart(ui.pfRate, { series: series, height: 190, yFmt: function (v) { return fmt(v, 2) + '%'; }, d: 2, csvName: 'policy_rate-' + iso,
        onClick: function (period) { openDrawer('policy_rate', shared ? 'EU' : iso, effFreq('policy_rate'), period, false); } });
    });

    // 중앙은행 기조 (미터 + 요약 + 인용 + 배지)
    loadInto(ui.pfStance, profile, function (d) {
      var doc = d.docs && d.docs.cb_stance;
      var pl = docPayload(doc), score = isNum(pl.stance_score) ? pl.stance_score : null;
      var mw = elem('div', 'meter-wrap');
      var ml = elem('div', 'meter-labels');
      ml.appendChild(elem('span', null, '비둘기파 (완화)'));
      ml.appendChild(elem('b', null, score === null ? '기조 지수 —' : '기조 지수 ' + sgn(score, 1) + ' · ' + stanceWord(score)));
      ml.appendChild(elem('span', null, '매파 (긴축)'));
      mw.appendChild(ml);
      var mb = elem('div', 'chart-box');
      mw.appendChild(mb);
      ui.pfStance.appendChild(mw);
      C.meter(mb, score);
      if (!doc) { ui.pfStance.appendChild(note('중앙은행 결정문 요약이 아직 없습니다.')); return; }
      var extra = elem('div', 'kv');
      var kv = [];
      if (pl.direction) kv.push('결정 ' + ({ hike: '인상', hold: '동결', cut: '인하' }[pl.direction] || pl.direction));
      if (pl.forward_guidance) kv.push('가이던스 ' + ({ tightening: '긴축', neutral: '중립', easing: '완화' }[pl.forward_guidance] || pl.forward_guidance));
      if (isNum(pl.rate_after)) kv.push('결정 후 금리 ' + fmt(pl.rate_after, 2) + '%');
      if (pl.statement_date) kv.push('성명 ' + pl.statement_date);
      extra.textContent = kv.join(' · ');
      ui.pfStance.appendChild(docCard(doc, { title: doc.title_ko || '최근 결정문 요약', aiLabel: 'AI 판정', extra: kv.length ? extra : null }));
    });

    // M2 YoY · 통화가치 타일
    loadInto(ui.pfMoney, Promise.all([get(seriesPath('m2_yoy', [iso])), get(fxPath()), profile]), function (arr) {
      var m2 = arr[0], fx = arr[1], prof = arr[2];
      var pts = (m2.series && m2.series[iso]) || [], last = lastFinite(pts), prev = prevFinite(pts);
      var m2Shared = Array.isArray(m2.euro_shared) && m2.euro_shared.indexOf(iso) >= 0;
      var t1 = C.tile('M2 전년비' + (m2Shared ? ' (유로존 공통)' : ''), last ? fmt(last.value, 1) + '%' : '—',
        last && prev ? '전기 대비 ' + sgn(last.value - prev.value, 1) + 'p · ' + last.period : (last ? last.period : '데이터 없음'),
        C.sparkline(tail(pts, 12), C.countryColor(iso, true)));
      if (last) { t1.classList.add('clickable'); t1.tabIndex = 0; t1.addEventListener('click', function () { openDrawer('m2_yoy', m2Shared ? 'EU' : iso, effFreq('m2_yoy'), last.period, false); }); }
      ui.pfMoney.appendChild(t1);
      if (iso === 'US') {
        var dxy = Array.isArray(fx.dxy) ? fx.dxy : [], dl = lastFinite(dxy), df = dxy.filter(function (p) { return isNum(p.value); })[0];
        ui.pfMoney.appendChild(C.tile('달러지수 (DXY)', dl ? fmt(dl.value, 1) : '—', dl && df ? '범위 시작 대비 ' + sgn((dl.value / df.value - 1) * 100, 1) + '%' : '', C.sparkline(tail(dxy, 12), C.countryColor('US'))));
      } else {
        var key = fx.series && fx.series[iso] ? iso : (prof.euro_ref && fx.series && fx.series[prof.euro_ref] ? prof.euro_ref : (c.euro && fx.series && fx.series.EU ? 'EU' : null));
        var fs = key ? fx.series[key] : [], fl = lastFinite(fs);
        var ch = fx.change && isNum(fx.change[key]) ? fx.change[key] : (fl ? fl.value - 100 : null);
        var latestFx = prof.latest && prof.latest.fx_usd && isNum(prof.latest.fx_usd.value) ? prof.latest.fx_usd.value : null;
        var ccy = (prof.country && prof.country.ccy) || c.ccy;
        ui.pfMoney.appendChild(C.tile('통화가치 지수 (범위 시작=100)' + (key && key !== iso ? ' · 유로존 공통' : ''), fl ? fmt(fl.value, 1) : '—',
          (isNum(ch) ? '범위 시작 대비 ' + sgn(ch, 1) + '%' : '') + (latestFx ? ' · ' + ccy + '/USD ' + fmt(latestFx, latestFx > 100 ? 0 : 2) : ''),
          C.sparkline(tail(fs, 12), C.countryColor(iso, true))));
      }
    });

    // 재정·규모 타일
    loadInto(ui.pfFiscal, profile, function (d) {
      var L = d.latest || {};
      function lat(id) { return L[id] || null; }
      function val(id) { var o = lat(id); return o && isNum(o.value) ? o.value : null; }
      var gdp = toUsd(val('gdp_usd'), indicatorOf('gdp_usd').unit), gni = toUsd(val('gni_usd'), indicatorOf('gni_usd').unit);
      var expGdp = val('gov_expense_gdp'), budget = isNum(gdp) && isNum(expGdp) ? gdp * expGdp / 100 : null;
      var milShare = val('mil_expenditure_share'), milGdp = val('mil_gdp'), milUsd = toUsd(val('mil_usd'), indicatorOf('mil_usd').unit);
      var per = function (id) { var o = lat(id); return o && o.period ? ' (' + o.period + ')' : ''; };
      var t;
      t = C.tile('명목 GDP' + per('gdp_usd'), usdAbbrev(gdp), isNum(val('gdp_growth')) ? '실질성장 ' + fmt(val('gdp_growth'), 1) + '%' + per('gdp_growth') : 'World Bank', null, krwSub(gdp, L, iso));
      tileClick(t, 'gdp_usd', lat('gdp_usd'), iso); ui.pfFiscal.appendChild(t);
      t = C.tile('GNI' + per('gni_usd'), usdAbbrev(gni), isNum(val('gni_pc')) ? '1인당 ' + usdAbbrev(val('gni_pc')) : 'World Bank', null, krwSub(gni, L, iso));
      tileClick(t, 'gni_usd', lat('gni_usd'), iso); ui.pfFiscal.appendChild(t);
      t = C.tile('정부 지출' + per('gov_expense_gdp'), budget !== null ? usdAbbrev(budget) : (isNum(expGdp) ? fmt(expGdp, 1) + '% GDP' : '—'),
        [isNum(expGdp) ? '지출/GDP ' + fmt(expGdp, 1) + '%' : null, isNum(val('gov_debt_gdp')) ? '부채/GDP ' + fmt(val('gov_debt_gdp'), 0) + '%' : null].filter(Boolean).join(' · '), null, krwSub(budget, L, iso));
      tileClick(t, 'gov_expense_gdp', lat('gov_expense_gdp'), iso); ui.pfFiscal.appendChild(t);
      t = C.tile('국방비/정부지출' + per('mil_expenditure_share'), isNum(milShare) ? fmt(milShare, 1) + '%' : '—',
        [isNum(milGdp) ? 'GDP 대비 ' + fmt(milGdp, 1) + '%' : null, isNum(milUsd) ? usdAbbrev(milUsd) : null, 'SIPRI'].filter(Boolean).join(' · '));
      tileClick(t, 'mil_expenditure_share', lat('mil_expenditure_share'), iso); ui.pfFiscal.appendChild(t);
      var flagsAll = [];
      ['gdp_usd', 'gni_usd', 'gov_expense_gdp', 'mil_expenditure_share'].forEach(function (id) { var o = lat(id); if (o && Array.isArray(o.flags)) o.flags.forEach(function (f) { if (flagsAll.indexOf(f) < 0) flagsAll.push(f); }); });
      if (flagsAll.length) { var fb = elem('div', 'tiles-flags'); fb.appendChild(flagBadges(flagsAll)); ui.pfFiscal.appendChild(fb); }
    });

    // 국방비/정부지출 연간 라인
    loadInto(ui.pfMil, get(seriesPath('mil_expenditure_share', [iso], 'Y')), function (d) {
      ui.pfMil.appendChild(note('국방비/정부지출 추이 (%, 연간 · World Bank/SIPRI)' + (state.freq !== 'Y' ? ' — 연간만 제공' : '') + ' · 점 클릭 시 관측치 상세'));
      C.lineChart(ui.pfMil, { series: [{ name: '국방비/정부지출', color: C.countryColor(iso, true), values: (d.series && d.series[iso]) || [] }], height: 150, yFmt: function (v) { return fmt(v, 1) + '%'; }, d: 1, csvName: 'mil_expenditure_share-' + iso,
        onClick: function (period) { openDrawer('mil_expenditure_share', iso, 'Y', period, false); } });
    });

    // 경제구조
    loadInto(ui.pfStructure, profile, function (d) { renderStructure(ui.pfStructure, iso, d.latest || {}); });
    // 에너지
    loadInto(ui.pfEnergy, profile, function (d) { renderEnergy(ui.pfEnergy, iso, d.latest || {}, d.docs || {}); });
    // 시장경제 3패널
    var mkIds = ['ppi_yoy', 'cpi_yoy', 'house_price_yoy'];
    loadInto(ui.pfMarket, Promise.all(mkIds.map(function (id) { return get(seriesPath(id, [iso])); })), function (arr) {
      var all = [0];
      arr.forEach(function (d) { ((d.series && d.series[iso]) || []).forEach(function (p) { if (isNum(p.value)) all.push(p.value); }); });
      var lo = Math.min.apply(null, all), hi = Math.max.apply(null, all);
      var grid = elem('div', 'grid3');
      ui.pfMarket.appendChild(grid);
      var notes = [];
      var SHORT = { ppi_yoy: 'PPI', cpi_yoy: 'CPI', house_price_yoy: '주택가격' };
      mkIds.forEach(function (id, i) {
        var pts = (arr[i].series && arr[i].series[iso]) || [], last = lastFinite(pts);
        var box = elem('div', 'sm'), hh = elem('div', 'h');
        var bb = elem('b', null, SHORT[id] || indName(id));
        bb.title = indName(id);
        hh.appendChild(bb);
        hh.appendChild(elem('span', null, last ? sgn(last.value, 1) + '%' : '—'));
        box.appendChild(hh);
        var cb = elem('div', 'chart-box');
        box.appendChild(cb);
        grid.appendChild(box);
        var ef = effFreq(id), fn = freqNote(id, ef);
        if (fn) notes.push(fn);
        C.lineChart(cb, { series: [{ name: indName(id), color: C.countryColor(iso, true), values: pts }], height: 150, yFmt: function (v) { return fmt(v, 0) + '%'; }, baseline: 0, area: true, endLabel: false, table: false, d: 1, yMin: lo, yMax: hi, yTicks: 4,
          onClick: function (period) { openDrawer(id, iso, ef, period, false); } });
      });
      notes.forEach(function (n) { ui.pfMarket.appendChild(note(n)); });
      // 표 보기(3지표 합본)
      var series = mkIds.map(function (id, i) { return { name: indName(id), values: (arr[i].series && arr[i].series[iso]) || [] }; });
      var labels = C.unionPeriods(series);
      var rows = labels.map(function (l) { return [l].concat(series.map(function (s) { var p = s.values.filter(function (x) { return String(x.period) === l; })[0]; return p && isNum(p.value) ? fmt(p.value, 1) : '—'; })); });
      C.tableView(ui.pfMarket, ['기간'].concat(series.map(function (s) { return s.name; })), rows, { csvName: 'market-' + iso });
    });
  }

  function tileClick(t, indicator, latestObj, iso) {
    if (!latestObj || !latestObj.period) return;
    t.classList.add('clickable');
    t.tabIndex = 0;
    t.title = '관측치 상세 보기';
    var open = function () { openDrawer(indicator, iso, latestObj.freq || 'Y', latestObj.period, false); };
    t.addEventListener('click', open);
    t.addEventListener('keydown', function (ev) { if (ev.key === 'Enter') open(); });
  }

  function stanceWord(s) {
    if (!isNum(s)) return '—';
    return s <= -1 ? '완화' : s < 0 ? '완화 쪽 중립' : s === 0 ? '중립' : s < 1 ? '긴축 쪽 중립' : '긴축';
  }

  // 정당 지지율 블록 (국가 프로필·정치 화면 공용): 라인(최대 4당+기타) + 여론조사 표
  function renderPartyBlock(box, badgeBox, iso, pol, height) {
    if (badgeBox) badgeBox.textContent = '';
    if (pol.not_applicable) {
      var pl = docPayload(pol.not_applicable);
      box.appendChild(elem('p', 'empty-msg', pl.reason || '경쟁 정당이 없는 체제 — 정당 지지율을 제공하지 않습니다.'));
      return;
    }
    var series = pol.series || {}, names = Object.keys(series);
    var scored = names.map(function (n) { var l = lastFinite(series[n]); return { name: n, pts: series[n] || [], last: l ? l.value : -Infinity }; })
      .sort(function (a, b) { return b.last - a.last; });
    var top = scored.slice(0, 4), rest = scored.slice(4);
    var lines = top.map(function (s, i) { return { name: s.name, color: C.COLORS.SLOT[i], values: aggMean(s.pts, state.freq) }; });
    if (rest.length) {
      var sum = {};
      rest.forEach(function (s) { s.pts.forEach(function (p) { if (p && isNum(p.value)) sum[p.period] = (sum[p.period] || 0) + p.value; }); });
      lines.push({ name: '기타', color: C.COLORS.OTHER, emph: false, values: aggMean(Object.keys(sum).sort().map(function (k) { return { period: k, value: sum[k] }; }), state.freq) });
    }
    var polls = Array.isArray(pol.polls) ? pol.polls : [];
    var anyAi = polls.some(function (p) { return p && p.ai_generated; });
    if (badgeBox && anyAi) badgeBox.appendChild(badge('AI 추출', 'badge-blue'));
    var pp = docPayload(pol.poll_of_polls);
    if (isNum(pp.n_polls)) box.appendChild(note('여론조사 평균 ' + (pp.asof ? pp.asof + ' 기준 · ' : '') + fmt(pp.n_polls, 0) + '건' + (pp.window_days ? ' (' + pp.window_days + '일 창)' : '') + (state.freq !== 'M' ? ' · 월 시계열을 ' + FREQ_LABEL[state.freq] + ' 평균으로 표시' : '')));
    if (!lines.length) { box.appendChild(elem('p', 'empty-msg', '정당 지지율 시계열이 아직 없습니다.')); }
    else {
      var cb = elem('div', 'chart-box');
      box.appendChild(cb);
      C.lineChart(cb, { series: lines, height: height || 200, yFmt: function (v) { return fmt(v, 0) + '%'; }, d: 1, csvName: 'party_support-' + iso });
    }
    if (polls.length) {
      var partyCols = top.slice(0, 2).map(function (s) { return s.name; });
      var tw = elem('div', 'table-wrap polls'), tb = elem('table', 'tbl'), thead = elem('thead'), h = elem('tr');
      ['조사기관', '조사 종료', '표본'].concat(partyCols).concat(['정부 지지율', '출처']).forEach(function (t, i) { h.appendChild(elem('th', i >= 2 && i < 5 + partyCols.length - 1 ? 'num' : null, t)); });
      thead.appendChild(h); tb.appendChild(thead);
      var tbody = elem('tbody');
      polls.slice(0, 12).forEach(function (p) {
        var d = docPayload(p), t = elem('tr');
        t.appendChild(elem('td', null, d.pollster || p.source_name || '—'));
        t.appendChild(elem('td', 'mono', d.fieldwork_end || p.date || '—'));
        t.appendChild(elem('td', 'num', isNum(d.sample_size) ? fmt(d.sample_size, 0) : '—'));
        partyCols.forEach(function (pc) { var v = d.results && isNum(d.results[pc]) ? d.results[pc] : null; t.appendChild(elem('td', 'num', v === null ? '—' : fmt(v, 0) + '%')); });
        t.appendChild(elem('td', 'num', isNum(d.gov_approval) ? fmt(d.gov_approval, 0) + '%' : '—'));
        var td = elem('td');
        if (p.source_url) td.appendChild(extLink(p.source_url, '원문'));
        // AI 추출 여부는 카드 머리글 배지로 한 번만 표시하고, 행에는 승인 전 검토 상태만 배지로 붙인다
        var rs = REVIEW_LABEL[p.review_status];
        if (rs && p.review_status !== 'approved') { td.appendChild(document.createTextNode(' ')); td.appendChild(badge(rs[0], rs[1])); }
        t.appendChild(td);
        tbody.appendChild(t);
      });
      tb.appendChild(tbody); tw.appendChild(tb); box.appendChild(tw);
    }
  }

  function payloadItems(obj) {
    var pl = obj && obj.payload;
    if (!pl) return [];
    var items = Array.isArray(pl.items) ? pl.items : (Array.isArray(pl) ? pl : []);
    return items.filter(Boolean).map(function (it) {
      var label = it.label !== undefined ? it.label : (it.name !== undefined ? it.name : (it.ticker !== undefined ? it.ticker : String(it)));
      var value = isNum(it.value) ? it.value : (isNum(it.share) ? it.share : (isNum(it.market_cap_usd_bn) ? it.market_cap_usd_bn : (isNum(it.market_cap_usd) ? it.market_cap_usd / 1e9 : (isNum(it.market_cap) ? it.market_cap : null))));
      return { label: String(label), value: value, sector: it.sector || it.industry || it.hs2 || null, raw: it };
    });
  }

  function renderStructure(box, iso, L) {
    function v(id) { var o = L[id]; return o && isNum(o.value) ? o.value : null; }
    var agri = v('va_agri'), ind = v('va_industry'), man = v('va_manuf'), svc = v('va_services');
    var segsRaw = [
      ['농림어업', agri, C.COLORS.SLOT[2]],
      ['제조업', man, C.COLORS.SLOT[0]],
      ['기타 산업(건설·광업·전기)', isNum(ind) && isNum(man) ? Math.max(0, ind - man) : (isNum(ind) ? ind : null), C.COLORS.SLOT[6]],
      ['서비스', svc, C.COLORS.SLOT[1]]
    ];
    var segs = segsRaw.filter(function (s) { return isNum(s[1]) && s[1] > 0; }).map(function (s) { return { label: s[0], value: s[1], color: s[2] }; });
    var sum = segs.reduce(function (a, b) { return a + b.value; }, 0);
    if (segs.length && sum < 99) segs.push({ label: '기타(순생산물세 등)', value: 100 - sum, color: C.COLORS.OTHER });
    var exportsGdp = v('exports_gdp');
    var per = L.va_services && L.va_services.period ? ' · ' + L.va_services.period : '';
    box.appendChild(note('산업별 부가가치 비중 (GDP, %)' + per + (isNum(exportsGdp) ? ' · 수출/GDP ' + fmt(exportsGdp, 0) + '%' : '')));
    var sb = elem('div', 'chart-box');
    box.appendChild(sb);
    if (segs.length) C.stackedH(sb, { title: '산업 비중', segments: segs, labelHead: '부문', csvName: 'value_added-' + iso });
    else sb.appendChild(elem('p', 'empty-msg', '산업 비중 데이터가 없습니다.'));
    var g = elem('div', 'grid2 tight');
    box.appendChild(g);
    // 상위 수출 품목 — 차트는 clientWidth를 읽으므로 반드시 DOM에 붙인 뒤 그린다
    var ex = elem('div');
    g.appendChild(ex);
    var exItems = payloadItems(L.exports_top_hs2);
    ex.appendChild(note('상위 수출 품목 (총수출 대비 %, WITS HS2' + (L.exports_top_hs2 && L.exports_top_hs2.period ? ' · ' + L.exports_top_hs2.period : '') + ')'));
    var eb = elem('div', 'chart-box');
    ex.appendChild(eb);
    if (exItems.length) C.hbars(eb, { items: exItems.slice(0, 8).map(function (e) { return { label: e.label, value: e.value, color: C.countryColor(iso, true) }; }), fmt: function (x) { return fmt(x, 1) + '%'; }, labelW: 96, unitLabel: '비중 %', labelHead: '품목', csvName: 'exports_top_hs2-' + iso });
    else eb.appendChild(elem('p', 'empty-msg', '수출 품목 데이터가 없습니다.'));
    // 시총 상위 기업
    var co = elem('div');
    g.appendChild(co);
    var coItems = payloadItems(L.top_companies);
    co.appendChild(note('시총 상위 기업 (십억 USD' + (L.top_companies && L.top_companies.period ? ' · ' + L.top_companies.period : '') + ')'));
    if (coItems.length) {
      var tw = elem('div', 'table-wrap companies'), tb = elem('table', 'tbl'), thead = elem('thead'), h = elem('tr');
      ['기업', '업종', '시총'].forEach(function (t, i) { h.appendChild(elem('th', i === 2 ? 'num' : null, t)); });
      thead.appendChild(h); tb.appendChild(thead);
      var tbody = elem('tbody');
      coItems.slice(0, 10).forEach(function (r) {
        var t = elem('tr');
        t.appendChild(elem('td', null, r.label));
        t.appendChild(elem('td', 'muted', r.sector || '—'));
        t.appendChild(elem('td', 'num', isNum(r.value) ? fmt(r.value, 0) : '—'));
        tbody.appendChild(t);
      });
      tb.appendChild(tbody); tw.appendChild(tb); co.appendChild(tw);
    } else co.appendChild(elem('p', 'empty-msg', '기업 데이터가 없습니다.'));
  }

  function energyColor(label, i) {
    for (var k = 0; k < ENERGY_COLORS.length; k++) if (ENERGY_COLORS[k][0].test(label)) return ENERGY_COLORS[k][1];
    return C.COLORS.SLOT[i % 8];
  }

  function renderEnergy(box, iso, L, docs) {
    function v(id) { var o = L[id]; return o && isNum(o.value) ? o.value : null; }
    var mix = payloadItems(L.elec_mix);
    box.appendChild(note('발전 믹스 (전력 생산 비중 %, Ember' + (L.elec_mix && L.elec_mix.period ? ' ' + L.elec_mix.period : '') + ')'));
    var mb = elem('div', 'chart-box');
    box.appendChild(mb);
    if (mix.length) C.stackedH(mb, { title: '발전 믹스', segments: mix.map(function (m, i) { return { label: m.label, value: m.value, color: energyColor(m.label, i) }; }), labelHead: '발전원', csvName: 'elec_mix-' + iso });
    else mb.appendChild(elem('p', 'empty-msg', '발전 믹스 데이터가 없습니다.'));
    var deps_ = [['원유', 'fuel_dep_oil'], ['천연가스', 'fuel_dep_gas'], ['석탄', 'fuel_dep_coal'], ['1차에너지 전체', 'energy_import_dep']]
      .map(function (d) { return { label: d[0], value: v(d[1]), color: C.COLORS.SLOT[3], id: d[1], obj: L[d[1]] }; });
    var anyDep = deps_.some(function (d) { return isNum(d.value); });
    var db = elem('div', 'chart-box gap-top');
    box.appendChild(db);
    if (anyDep) {
      C.hbars(db, { items: deps_, fmt: function (x) { return fmt(x, 0) + '%'; }, max: 100, labelW: 110, unitLabel: '해외의존도 %', labelHead: '연료', csvName: 'fuel_dep-' + iso,
        onClick: function (it) { if (it.obj && it.obj.period) openDrawer(it.id, iso, it.obj.freq || 'Y', it.obj.period, false); } });
      var fl = [];
      deps_.forEach(function (d) { if (d.obj && Array.isArray(d.obj.flags)) d.obj.flags.forEach(function (f) { if (fl.indexOf(f) < 0) fl.push(f); }); });
      var n = note('해외의존도 = 1 − 생산/소비 (OWID) · 100% = 전량 수입 ');
      if (fl.length) n.appendChild(flagBadges(fl));
      box.appendChild(n);
    } else db.appendChild(elem('p', 'empty-msg', '연료별 해외의존도 데이터가 없습니다.'));
    var doc = docs.energy_policy;
    var pl = docPayload(doc), extra = elem('div');
    if (Array.isArray(pl.targets) && pl.targets.length) { extra.appendChild(elem('div', 'kv-title', '목표')); var ul = elem('ul', 'brief-list'); pl.targets.forEach(function (t) { ul.appendChild(elem('li', null, String(t))); }); extra.appendChild(ul); }
    if (Array.isArray(pl.recent_changes) && pl.recent_changes.length) { extra.appendChild(elem('div', 'kv-title', '최근 변화')); var ul2 = elem('ul', 'brief-list'); pl.recent_changes.forEach(function (t) { ul2.appendChild(elem('li', null, String(t))); }); extra.appendChild(ul2); }
    if (Array.isArray(pl.sources) && pl.sources.length) { var sl = elem('div', 'doc-link'); pl.sources.forEach(function (u, i) { if (i) sl.appendChild(document.createTextNode(' · ')); sl.appendChild(extLink(String(u), '출처 ' + (i + 1))); }); extra.appendChild(sl); }
    var dc = docCard(doc, { title: (doc && doc.title_ko) || '에너지 정책 요약', aiLabel: 'AI 요약', extra: extra, emptyText: '에너지 정책 요약이 아직 없습니다.' });
    dc.classList.add('gap-top');
    box.appendChild(dc);
  }

  // ---------- ③ 지표 비교 ----------
  function renderIndicator() {
    Array.prototype.forEach.call(ui.indChips.querySelectorAll('.chip'), function (b) { b.classList.toggle('on', state.indCountries.indexOf(b.dataset.iso) >= 0); });
    if (ui.indSelect.value !== state.indicator) ui.indSelect.value = state.indicator;
    // 강조 select: 선택 국가 + 전체 색상
    var cs = state.indCountries.slice();
    if (state.emph !== 'none' && cs.indexOf(state.emph) < 0) state.emph = cs[0] || 'none';
    ui.indEmph.textContent = '';
    cs.forEach(function (iso) { var o = elem('option', null, countryName(iso)); o.value = iso; ui.indEmph.appendChild(o); });
    var oNone = elem('option', null, '전체 색상 (고정색 8슬롯 · 그 외 회색)'); oNone.value = 'none'; ui.indEmph.appendChild(oNone);
    ui.indEmph.value = state.emph;

    var im = indicatorOf(state.indicator), ef = effFreq(state.indicator);
    var isIndex = state.transform === 'index';
    ui.indTitle.textContent = (im.name_ko || state.indicator) + (isIndex ? ' — 지수화 (범위 시작=100)' : (im.unit ? ' (' + unitLabel(im.unit) + ')' : '')) + ' · ' + FREQ_LABEL[ef] + ' · ' + RANGE_LABEL[state.range];
    ui.indNote.textContent = '';
    var fn = freqNote(state.indicator, ef);
    if (fn) ui.indNote.appendChild(note(fn));
    if (!cs.length) { ui.indChart.textContent = ''; ui.indChart.appendChild(elem('p', 'empty-msg', '비교할 국가를 선택하세요.')); return; }

    var d = isNum(im.decimals) ? im.decimals : 1;
    var yFmt = function (v) { return fmt(v, d > 1 ? 1 : d) + (isIndex || !im.unit || im.unit !== '%' ? '' : '%'); };
    loadInto(ui.indChart, get(seriesPath(state.indicator, cs, null, state.transform)), function (data) {
      var S = data.series || {};
      if (Array.isArray(data.euro_shared) && data.euro_shared.length) {
        var nb = note('유로존 공통 값 참조: ' + data.euro_shared.map(function (i) { return countryOf(i).name_ko; }).join(', ') + ' ');
        nb.appendChild(badge('유로존 공통', 'badge-gray'));
        ui.indChart.appendChild(nb);
      }
      if (cs.length > 8) {
        var g = elem('div', 'sm-grid');
        ui.indChart.appendChild(g);
        cs.forEach(function (iso) {
          var pts = S[iso] || [], last = lastFinite(pts);
          var sm = elem('div', 'sm'), h = elem('div', 'h');
          h.appendChild(elem('b', null, countryName(iso)));
          h.appendChild(elem('span', null, last ? fmt(last.value, d) : '—'));
          sm.appendChild(h);
          var cb = elem('div', 'chart-box');
          sm.appendChild(cb);
          g.appendChild(sm);
          C.lineChart(cb, { series: [{ name: countryOf(iso).name_ko, color: C.countryColor(iso, true), values: pts }], height: 120, yFmt: function (v) { return fmt(v, d > 1 ? 1 : d); }, endLabel: false, table: false, baseline: isIndex ? 100 : null,
            onClick: function (period) { openDrawer(state.indicator, iso, ef, period, false); } });
        });
        ui.indChart.appendChild(note('8개국 초과 → 소형 다중 패널 (색 추가 생성 금지 규칙)'));
        // 합본 표
        var series = cs.map(function (iso) { return { name: countryOf(iso).name_ko, values: S[iso] || [] }; });
        var labels = C.unionPeriods(series);
        C.tableView(ui.indChart, ['기간'].concat(series.map(function (s) { return s.name; })), labels.map(function (l) {
          return [l].concat(series.map(function (s) { var p = s.values.filter(function (x) { return String(x.period) === l; })[0]; return p && isNum(p.value) ? fmt(p.value, d) : '—'; }));
        }), { csvName: state.indicator + '-compare' });
        return;
      }
      var lines = cs.map(function (iso, i) {
        var emph = state.emph === 'none' ? true : iso === state.emph;
        var color = state.emph === 'none' ? (C.COLORS.BY_ISO[iso] || C.COLORS.OTHER) : (emph ? C.countryColor(iso, true) : C.COLORS.OTHER);
        return { name: countryName(iso), color: color, values: S[iso] || [], emph: emph, iso: iso };
      });
      lines.sort(function (a, b) { return (a.emph === false ? 0 : 1) - (b.emph === false ? 0 : 1); });
      var cb = elem('div', 'chart-box');
      ui.indChart.appendChild(cb);
      C.lineChart(cb, { series: lines, height: 340, yFmt: yFmt, d: d, baseline: isIndex ? 100 : null, csvName: state.indicator + '-compare',
        onClick: function (period) { var target = state.emph === 'none' ? cs[0] : state.emph; openDrawer(state.indicator, target, ef, period, false); } });
    });
  }

  // ---------- ④ 통화가치 ----------
  // 표시할 통화: 현재 국가군 순서대로, 미국(기준통화)·유로 회원국(EUR 중복) 제외.
  // 국가군에 유로 회원국이 있으면(G7 등) 유로존(EU)을 대신 포함해 EUR가 빠지지 않게 한다.
  function fxIsos(S) {
    var grp = groupCountries(), isos = [];
    grp.forEach(function (c) { if (c.iso !== 'US' && !c.euro && S[c.iso]) isos.push(c.iso); });
    if (isos.indexOf('EU') < 0 && S.EU && grp.some(function (c) { return c.euro; })) isos.push('EU');
    return isos;
  }

  function renderFx() {
    ui.fxBase.textContent = '';
    ui.fxBase.appendChild(badge('기준 ' + fromDate(), 'badge-gray'));
    var p = get(fxPath());
    loadInto(ui.fxGrid, p, function (data) {
      var S = data.series || {};
      var isos = fxIsos(S);
      if (!isos.length) { ui.fxGrid.appendChild(elem('p', 'empty-msg', '통화가치 데이터가 없습니다.')); return; }
      if (data.base) { ui.fxBase.textContent = ''; ui.fxBase.appendChild(badge('기준 ' + data.base, 'badge-gray')); }
      var grid = elem('div', 'sm-grid');
      ui.fxGrid.appendChild(grid);
      isos.forEach(function (iso) {
        var c = countryOf(iso), pts = S[iso] || [], last = lastFinite(pts);
        var ch = data.change && isNum(data.change[iso]) ? data.change[iso] : (last ? last.value - 100 : null);
        var sm = elem('div', 'sm'), h = elem('div', 'h');
        h.appendChild(elem('b', null, flag(iso) + ' ' + (c.ccy || iso) + ' · ' + c.name_ko));
        h.appendChild(elem('span', null, last ? fmt(last.value, 1) + ' (' + sgn(ch, 1) + '%)' : '—'));
        sm.appendChild(h);
        var cb = elem('div', 'chart-box');
        sm.appendChild(cb);
        grid.appendChild(sm);
        C.lineChart(cb, { series: [{ name: c.ccy || iso, color: C.countryColor(iso, true), values: pts }], height: 110, yFmt: function (v) { return fmt(v, 0); }, baseline: 100, area: true, endLabel: false, table: false,
          onClick: function (period) { openDrawer('fx_value_index', iso, state.freq === 'Y' ? 'M' : state.freq, period, false); } });
      });
      // 합본 표
      var series = isos.map(function (iso) { return { name: countryOf(iso).ccy || iso, values: S[iso] || [] }; });
      var labels = C.unionPeriods(series);
      C.tableView(ui.fxGrid, ['기간'].concat(series.map(function (s) { return s.name; })), labels.map(function (l) {
        return [l].concat(series.map(function (s) { var pt = s.values.filter(function (x) { return String(x.period) === l; })[0]; return pt && isNum(pt.value) ? fmt(pt.value, 1) : '—'; }));
      }), { csvName: 'fx_value_index' });
    });
    loadInto(ui.fxBars, p, function (data) {
      var S = data.series || {}, ch = data.change || {};
      var items = fxIsos(S).map(function (iso) {
        var last = lastFinite(S[iso]);
        return { label: flag(iso) + ' ' + (countryOf(iso).ccy || iso), value: isNum(ch[iso]) ? ch[iso] : (last ? last.value - 100 : null), iso: iso };
      }).filter(function (i) { return isNum(i.value); });
      items.sort(function (a, b) { return b.value - a.value; });
      C.divBars(ui.fxBars, { items: items, valueLabel: '통화가치 변화 %', negLabel: '달러 대비 약세(음)', posLabel: '달러 대비 강세(양)', labelHead: '통화', csvName: 'fx_change' });
    });
    loadInto(ui.fxDxy, p, function (data) {
      var dxy = Array.isArray(data.dxy) ? data.dxy : [];
      C.lineChart(ui.fxDxy, { series: [{ name: 'DXY', color: C.countryColor('US'), values: dxy }], height: 220, yFmt: function (v) { return fmt(v, 0); }, d: 1, csvName: 'dxy',
        onClick: function (period) { openDrawer('dxy', 'US', state.freq === 'Y' ? 'M' : state.freq, period, false); } });
      ui.fxDxy.appendChild(note('출처: BIS WS_XRU(월) · Yahoo Finance(일) · DXY는 FRED/Yahoo'));
    });
  }

  // ---------- ⑤ 정치 ----------
  function confidenceOf(iso, pp) {
    if (iso === 'RU') return ['낮음', 'badge-red'];
    if (!pp || !isNum(pp.n_polls)) return null;
    return pp.n_polls >= 5 ? ['높음', 'badge-green'] : pp.n_polls >= 2 ? ['보통', 'badge-yellow'] : ['낮음', 'badge-red'];
  }

  function renderPolitics() {
    var iso = state.polCountry;
    var p = get('/macro/overview' + qs({ group: state.group, indicators: 'party_support,gov_approval' }));
    loadInto(ui.polTable, p, function (data) { renderPolTable(ui.polTable, data); });
    ui.polDetailTitle.textContent = countryName(iso) + ' 정당 지지율';
    var profile = get('/macro/countries/' + encodeURIComponent(iso));
    profile.then(function (d) {
      profiles[iso] = d;
      // 프로필이 도착하면 표의 해당 행을 채운다 (점진 로딩)
      if (state.view === 'politics') p.then(function (data) { if (ui.polTable.offsetParent) { ui.polTable.textContent = ''; renderPolTable(ui.polTable, data); } }, function () {});
    }, function () {});
    loadInto(ui.polDetail, Promise.all([get('/macro/politics/' + encodeURIComponent(iso) + qs({ from: fromPeriod('M') })), profile]), function (arr) {
      var pol = arr[0], prof = arr[1], docs = prof.docs || {}, el = docPayload(docs.election), pp = docPayload(pol.poll_of_polls || docs.poll_of_polls);
      var meta_ = elem('div', 'meta profile-meta');
      [['집권', el.ruling_party || pp.ruling_party || (NO_PARTY[iso] ? '경쟁 정당 없음' : '—')], ['1위 정당', pp.leader_party ? pp.leader_party + (isNum(pp.leader_pct) ? ' ' + fmt(pp.leader_pct, 0) + '%' : '') : '—'],
        ['정부 지지율', isNum(pp.gov_approval) ? fmt(pp.gov_approval, 0) + '%' : '—'], ['다음 선거', el.next_election_date ? el.next_election_date + (el.election_type ? ' ' + el.election_type : '') : '—']]
        .forEach(function (m) { var s = elem('span'); s.appendChild(elem('b', null, m[0] + ' ')); s.appendChild(document.createTextNode(m[1])); meta_.appendChild(s); });
      if (el.system_note) meta_.appendChild(elem('span', 'muted', el.system_note));
      ui.polDetail.appendChild(meta_);
      renderPartyBlock(ui.polDetail, ui.polDetailBadge, iso, pol, 260);
    });
  }

  function renderPolTable(box, data) {
    var rows = Array.isArray(data.rows) ? data.rows : [];
    if (!rows.length) { box.appendChild(elem('p', 'empty-msg', '표시할 데이터가 없습니다.')); return; }
    var wrap = elem('div', 'table-wrap'), t = elem('table', 'tbl politics'), thead = elem('thead'), h = elem('tr');
    ['국가', '집권당 (성향)', '1위 정당 지지율', '정부 지지율', '다음 선거', '조사 신뢰도'].forEach(function (x, i) { h.appendChild(elem('th', i === 2 || i === 3 ? 'num' : null, x)); });
    thead.appendChild(h); t.appendChild(thead);
    var tb = elem('tbody');
    rows.forEach(function (r) {
      var iso = r.iso, cells = r.cells || {}, ps = cells.party_support, ga = cells.gov_approval;
      var prof = profiles[iso], docs = prof && prof.docs ? prof.docs : null;
      var el = docPayload(docs && docs.election), pp = docPayload(docs && docs.poll_of_polls);
      var pl = (ps && ps.payload) || {};
      var tr = elem('tr');
      if (iso === state.polCountry) tr.classList.add('selected');
      tr.tabIndex = 0;
      tr.setAttribute('role', 'button');
      tr.appendChild(elem('td', 'country', countryName(iso)));
      var ruling = el.ruling_party || pp.ruling_party || pl.ruling_party;
      var tdR = elem('td');
      if (ruling) tdR.textContent = ruling + (el.ruling_lean ? ' (' + el.ruling_lean + ')' : '');
      else if (NO_PARTY[iso] || (docs && docs.not_applicable)) tdR.appendChild(badge('경쟁 정당 없음', 'badge-gray'));
      else tdR.textContent = '—';
      tr.appendChild(tdR);
      var td = elem('td', 'num');
      if (ps && isNum(ps.value)) {
        var w = elem('span', 'bar-cell');
        var bar = elem('i');
        bar.style.width = Math.max(2, Math.min(100, ps.value)) * 1.6 + 'px';
        bar.style.background = C.countryColor(iso, true);
        w.appendChild(bar);
        var leader = pl.leader_party || pp.leader_party;
        w.appendChild(document.createTextNode((leader ? leader + ' ' : '') + fmt(ps.value, 0) + '%'));
        td.appendChild(w);
        td.classList.add('clickable');
        td.title = '관측치 상세';
        td.addEventListener('click', function (ev) { ev.stopPropagation(); if (ps.period) openDrawer('party_support', iso, ps.freq || 'M', ps.period, false); });
      } else td.textContent = NO_PARTY[iso] ? '해당 없음' : '—';
      tr.appendChild(td);
      tr.appendChild(elem('td', 'num', ga && isNum(ga.value) ? fmt(ga.value, 0) + '%' : (isNum(pp.gov_approval) ? fmt(pp.gov_approval, 0) + '%' : (NO_GOV[iso] || NO_PARTY[iso] ? '해당 없음' : '—'))));
      tr.appendChild(elem('td', null, el.next_election_date ? el.next_election_date + (el.election_type ? ' ' + el.election_type : '') : '—'));
      var tdT = elem('td');
      var conf = docs ? confidenceOf(iso, pp) : null;
      if (conf) tdT.appendChild(badge(conf[0], conf[1]));
      else tdT.textContent = NO_PARTY[iso] ? '해당 없음' : '—';
      tr.appendChild(tdT);
      function pick() { state.polCountry = iso; location.hash = viewHash('politics'); }
      tr.addEventListener('click', pick);
      tr.addEventListener('keydown', function (ev) { if (ev.key === 'Enter') pick(); });
      tb.appendChild(tr);
    });
    t.appendChild(tb); wrap.appendChild(t); box.appendChild(wrap);
    box.appendChild(note('행을 클릭하면 아래에 정당 지지율 추이와 여론조사 원자료를 표시하고 집권당·다음 선거·신뢰도를 채웁니다.'));
  }

  // ---------- 관측치 상세 드로어 ----------
  function openDrawer(indicator, iso, freq, period, fromRoute) {
    if (!indicator || !iso || !freq || !period) return;
    state.drawer = { indicator: indicator, iso: iso, freq: freq, period: period };
    if (!fromRoute) replaceHash(currentPathHash() + '?obs=' + encodeURIComponent([indicator, iso, freq, period].join(',')));
    var d = ui.drawer, body = ui.drBody;
    d.hidden = false;
    body.textContent = '';
    body.appendChild(elem('h3', null, countryName(iso) + ' · ' + indName(indicator)));
    body.appendChild(elem('p', 'empty-msg', '불러오는 중…'));
    var key = state.drawer;
    get('/macro/observations' + qs({ indicator: indicator, iso: iso, freq: freq, period: period })).then(function (data) {
      if (state.drawer !== key) return;
      body.textContent = '';
      renderDrawer(body, data, key);
    }, function (err) {
      if (state.drawer !== key) return;
      body.textContent = '';
      body.appendChild(elem('h3', null, countryName(iso) + ' · ' + indName(indicator)));
      body.appendChild(errMsg(err && err.message ? err.message : String(err)));
    });
    ui.drawer.querySelector('.close').focus();
  }

  function closeDrawer(fromRoute) {
    if (!ui.drawer) return;
    var wasOpen = !ui.drawer.hidden;
    ui.drawer.hidden = true;
    state.drawer = null;
    if (!fromRoute && wasOpen) replaceHash(currentPathHash());
  }

  function renderDrawer(body, data, key) {
    var o = data.observation || {};
    var im = indicatorOf(o.indicator || key.indicator);
    var d = isNum(im.decimals) ? im.decimals : 2;
    body.appendChild(elem('h3', null, countryName(o.iso || key.iso) + ' · ' + (im.name_ko || key.indicator)));
    var hero = elem('div', 'hero');
    if (isNum(o.value)) {
      hero.textContent = fmt(o.value, d);
      hero.appendChild(elem('small', null, (unitLabel(o.unit || im.unit) || '') + ' · ' + (o.period || key.period) + ' (' + (FREQ_LABEL[o.freq || key.freq] || key.freq) + ')'));
    } else {
      hero.textContent = '복합값';
      hero.appendChild(elem('small', null, (o.period || key.period) + ' (' + (FREQ_LABEL[o.freq || key.freq] || key.freq) + ')'));
    }
    body.appendChild(hero);
    if (Array.isArray(o.flags) && o.flags.length) { var fb = elem('div', 'gap-top'); fb.appendChild(flagBadges(o.flags)); body.appendChild(fb); }
    var items = payloadItems(o);
    if (items.length) {
      var tw = elem('div', 'table-wrap gap-top'), tb = elem('table', 'tbl'), thead = elem('thead'), h = elem('tr');
      ['항목', '값'].forEach(function (x, i) { h.appendChild(elem('th', i ? 'num' : null, x)); });
      thead.appendChild(h); tb.appendChild(thead);
      var tbody = elem('tbody');
      items.forEach(function (it) { var t = elem('tr'); t.appendChild(elem('td', null, it.label + (it.sector ? ' · ' + it.sector : ''))); t.appendChild(elem('td', 'num', isNum(it.value) ? fmt(it.value, 1) : '—')); tbody.appendChild(t); });
      tb.appendChild(tbody); tw.appendChild(tb); body.appendChild(tw);
    }
    var dl = elem('dl');
    function row(k, v, isLink) {
      dl.appendChild(elem('dt', null, k));
      var dd = elem('dd');
      if (isLink && v && v !== '—') dd.appendChild(extLink(v, v)); else dd.textContent = v === null || v === undefined || v === '' ? '—' : String(v);
      dl.appendChild(dd);
    }
    row('지표 ID', o.indicator || key.indicator);
    row('빈도', (FREQ_LABEL[o.freq || key.freq] || key.freq) + ' (' + (o.freq || key.freq) + ')');
    row('기간', o.period || key.period);
    row('단위', o.unit ? unitLabel(o.unit) + (UNIT_LABEL[o.unit] && UNIT_LABEL[o.unit] !== o.unit ? ' (' + o.unit + ')' : '') : '—');
    row('집계 방법', o.method);
    row('출처', o.source);
    row('시리즈 ID', o.series_id);
    row('URL', o.source_url || '—', true);
    row('수집 시각', o.retrieved_at ? fmtKST(o.retrieved_at) + ' KST' : '—');
    row('데이터 vintage', o.vintage);
    body.appendChild(dl);

    body.appendChild(elem('div', 'sect', '개정 이력'));
    var revs = Array.isArray(o.revisions) ? o.revisions : [];
    if (!revs.length) body.appendChild(note('개정 이력 없음 (최초 수집값)'));
    else {
      var rl = elem('dl');
      revs.slice().reverse().forEach(function (r, i) {
        rl.appendChild(elem('dt', null, r.vintage || (r.retrieved_at ? fmtKST(r.retrieved_at) : '—')));
        rl.appendChild(elem('dd', null, (isNum(r.value) ? fmt(r.value, d) : '—') + (i === 0 && isNum(r.value) && isNum(o.value) && r.value === o.value ? ' (현재)' : '')));
      });
      body.appendChild(rl);
    }

    body.appendChild(elem('div', 'sect', '관련 문서'));
    var docs = Array.isArray(data.related_docs) ? data.related_docs : [];
    if (!docs.length) body.appendChild(note('연결된 정성 문서 없음'));
    else docs.forEach(function (doc) { var dc = docCard(doc, { aiLabel: doc.type === 'poll' ? 'AI 추출' : 'AI 판정' }); dc.classList.add('gap-top'); body.appendChild(dc); });
  }

  function onKeydown(ev) {
    if (ev.key === 'Escape' && ui.drawer && !ui.drawer.hidden) closeDrawer(false);
  }

  function onResize() {
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () { resizeTimer = null; if (mounted && root.offsetParent) render(); }, 150);
  }

  // ---------- 공개 API ----------
  function mount(rootEl, d) {
    if (mounted && root === rootEl) return;
    deps = d;
    root = rootEl;
    buildSkeleton();
    mounted = true;
    document.addEventListener('keydown', onKeydown);
    window.addEventListener('resize', onResize);
    ensureMeta();
  }

  function unmount() {
    if (!mounted) return;
    document.removeEventListener('keydown', onKeydown);
    window.removeEventListener('resize', onResize);
    C.hideTip();
    if (root) root.textContent = '';
    mounted = false;
    ui = {};
  }

  window.MacroView = { mount: mount, route: route, unmount: unmount };
})();
