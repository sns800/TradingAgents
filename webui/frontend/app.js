/* TradingAgents 웹 UI */
(function () {
  'use strict';

  var API_BASE = '/api';
  var POLL_INTERVAL = 10000;

  // ---------- 상수 ----------

  var STATUS_LABEL = {
    queued: '대기중',
    running: '실행중',
    completed: '완료',
    failed: '실패'
  };

  var STATUS_BADGE = {
    queued: 'badge-gray',
    running: 'badge-blue',
    completed: 'badge-green',
    failed: 'badge-red'
  };

  var DECISION_BADGE = {
    BUY: 'badge-green',
    SELL: 'badge-red',
    HOLD: 'badge-yellow'
  };

  var DEPTH_LABEL = {
    1: '얕게',
    3: '중간',
    5: '깊게'
  };

  var REPORT_LABEL = {
    'complete_report.md': '전체 보고서',
    '1_analysts/market.md': '시장 분석',
    '1_analysts/sentiment.md': '감성 분석',
    '1_analysts/news.md': '뉴스 분석',
    '1_analysts/fundamentals.md': '펀더멘털 분석',
    '2_research/bull.md': '강세론',
    '2_research/bear.md': '약세론',
    '2_research/manager.md': '리서치 매니저',
    '3_trading/trader.md': '트레이더 제안',
    '4_risk/aggressive.md': '공격적 시각',
    '4_risk/conservative.md': '보수적 시각',
    '4_risk/neutral.md': '중립적 시각',
    '5_portfolio/decision.md': '최종 결정'
  };

  // ---------- DOM 참조 ----------

  var el = {
    viewList: document.getElementById('view-list'),
    viewDetail: document.getElementById('view-detail'),
    form: document.getElementById('new-run-form'),
    inputTicker: document.getElementById('input-ticker'),
    inputDate: document.getElementById('input-date'),
    inputDepth: document.getElementById('input-depth'),
    btnSubmit: document.getElementById('btn-submit'),
    formError: document.getElementById('form-error'),
    runsList: document.getElementById('runs-list'),
    listStatus: document.getElementById('list-status'),
    btnBack: document.getElementById('btn-back'),
    detailSummary: document.getElementById('detail-summary'),
    detailReports: document.getElementById('detail-reports'),
    reportNav: document.getElementById('report-nav'),
    reportContent: document.getElementById('report-content')
  };

  // ---------- 상태 ----------

  var state = {
    route: { view: 'list', runId: null },
    pollTimer: null,
    tickTimer: null,
    currentReport: null,
    reportsLoaded: false
  };

  // ---------- 유틸 ----------

  function todayStr() {
    var now = new Date();
    var y = now.getFullYear();
    var m = String(now.getMonth() + 1).padStart(2, '0');
    var d = String(now.getDate()).padStart(2, '0');
    return y + '-' + m + '-' + d;
  }

  function formatKST(iso) {
    if (!iso) return '-';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    try {
      return new Intl.DateTimeFormat('ko-KR', {
        timeZone: 'Asia/Seoul',
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', hour12: false
      }).format(d);
    } catch (e) {
      return d.toLocaleString('ko-KR');
    }
  }

  function formatElapsed(iso) {
    if (!iso) return '';
    var start = new Date(iso).getTime();
    if (isNaN(start)) return '';
    var sec = Math.max(0, Math.floor((Date.now() - start) / 1000));
    var h = Math.floor(sec / 3600);
    var m = Math.floor((sec % 3600) / 60);
    var s = sec % 60;
    if (h > 0) return h + '시간 ' + m + '분 ' + s + '초 경과';
    if (m > 0) return m + '분 ' + s + '초 경과';
    return s + '초 경과';
  }

  function reportLabel(name) {
    return REPORT_LABEL[name] || name;
  }

  function elem(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function statusBadge(status) {
    var badge = elem('span', 'badge ' + (STATUS_BADGE[status] || 'badge-gray'));
    if (status === 'running') {
      badge.appendChild(elem('span', 'spinner'));
    }
    badge.appendChild(document.createTextNode(STATUS_LABEL[status] || String(status)));
    return badge;
  }

  function decisionBadge(decision) {
    if (!decision) return null;
    var key = String(decision).toUpperCase();
    return elem('span', 'badge ' + (DECISION_BADGE[key] || 'badge-gray'), String(decision));
  }

  // ---------- API ----------

  function sha256Hex(text) {
    var bytes = new TextEncoder().encode(text);
    return crypto.subtle.digest('SHA-256', bytes).then(function (buf) {
      return Array.from(new Uint8Array(buf)).map(function (b) {
        return b.toString(16).padStart(2, '0');
      }).join('');
    });
  }

  function apiFetch(path, options) {
    // CloudFront OAC는 POST/PUT 본문을 서명하지 않으므로,
    // 본문이 있는 요청은 x-amz-content-sha256 헤더(본문 해시)를 함께 보내야 한다.
    var prepared;
    if (options && options.body) {
      prepared = sha256Hex(options.body).then(function (hash) {
        options.headers = options.headers || {};
        options.headers['x-amz-content-sha256'] = hash;
        return options;
      });
    } else {
      prepared = Promise.resolve(options);
    }
    return prepared.then(function (opts) {
      return doFetch(path, opts);
    });
  }

  function doFetch(path, options) {
    return fetch(API_BASE + path, options).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        try {
          data = JSON.parse(text);
        } catch (e) {
          data = null;
        }
        if (!res.ok) {
          var msg = (data && data.error) ? data.error : ('서버 오류 (HTTP ' + res.status + ')');
          throw new Error(msg);
        }
        if (data === null) {
          throw new Error('서버 응답을 해석할 수 없습니다.');
        }
        return data;
      });
    }, function () {
      throw new Error('네트워크 오류: 서버에 연결할 수 없습니다.');
    });
  }

  // ---------- 폴링 ----------

  function stopPolling() {
    if (state.pollTimer) {
      clearTimeout(state.pollTimer);
      state.pollTimer = null;
    }
  }

  function schedulePoll(fn) {
    stopPolling();
    state.pollTimer = setTimeout(fn, POLL_INTERVAL);
  }

  // ---------- 목록 화면 ----------

  function renderRuns(runs) {
    el.runsList.textContent = '';

    if (!Array.isArray(runs) || runs.length === 0) {
      el.runsList.appendChild(elem('p', 'empty-msg', '아직 실행된 분석이 없습니다. 위 폼에서 새 분석을 시작해 보세요.'));
      return;
    }

    runs.forEach(function (run) {
      var row = elem('div', 'run-row');
      row.setAttribute('role', 'button');
      row.tabIndex = 0;

      row.appendChild(elem('span', 'run-ticker', run.ticker || '-'));
      row.appendChild(elem('span', 'run-date', run.analysis_date || '-'));
      row.appendChild(elem('span', 'run-depth',
        '깊이: ' + (DEPTH_LABEL[run.depth] || run.depth || '-')));

      var meta = elem('div', 'run-meta');
      meta.appendChild(statusBadge(run.status));

      var dec = decisionBadge(run.decision);
      if (dec) meta.appendChild(dec);

      if (run.status === 'running' || run.status === 'queued') {
        var elapsed = elem('span', 'run-elapsed', formatElapsed(run.created_at));
        elapsed.dataset.created = run.created_at || '';
        meta.appendChild(elapsed);
      }

      meta.appendChild(elem('span', 'run-created', formatKST(run.created_at)));
      row.appendChild(meta);

      function goDetail() {
        location.hash = '#/runs/' + encodeURIComponent(run.run_id);
      }
      row.addEventListener('click', goDetail);
      row.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          goDetail();
        }
      });

      el.runsList.appendChild(row);
    });
  }

  function loadRuns() {
    apiFetch('/runs').then(function (data) {
      if (state.route.view !== 'list') return;
      renderRuns(data.runs || []);
      el.listStatus.textContent = '마지막 갱신: ' + formatKST(new Date().toISOString());
      schedulePoll(loadRuns);
    }).catch(function (err) {
      if (state.route.view !== 'list') return;
      el.listStatus.textContent = '갱신 실패: ' + err.message;
      schedulePoll(loadRuns);
    });
  }

  // ---------- 새 분석 폼 ----------

  function setFormError(msg) {
    if (msg) {
      el.formError.textContent = msg;
      el.formError.hidden = false;
    } else {
      el.formError.textContent = '';
      el.formError.hidden = true;
    }
  }

  el.form.addEventListener('submit', function (e) {
    e.preventDefault();
    setFormError(null);

    var ticker = el.inputTicker.value.trim().toUpperCase();
    var date = el.inputDate.value;
    var depth = parseInt(el.inputDepth.value, 10);

    if (!ticker) {
      setFormError('종목 코드를 입력해 주세요.');
      return;
    }
    if (!date) {
      setFormError('분석 날짜를 선택해 주세요.');
      return;
    }
    if (date > todayStr()) {
      setFormError('미래 날짜는 선택할 수 없습니다.');
      return;
    }

    el.btnSubmit.disabled = true;
    el.btnSubmit.textContent = '요청 중…';

    apiFetch('/runs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ticker: ticker, analysis_date: date, depth: depth })
    }).then(function (data) {
      el.inputTicker.value = '';
      if (data && data.run_id) {
        location.hash = '#/runs/' + encodeURIComponent(data.run_id);
      } else {
        loadRuns();
      }
    }).catch(function (err) {
      setFormError(err.message);
    }).finally(function () {
      el.btnSubmit.disabled = false;
      el.btnSubmit.textContent = '분석 시작';
    });
  });

  // ---------- 상세 화면 ----------

  function renderDetailSummary(run) {
    var box = el.detailSummary;
    box.textContent = '';

    var titleRow = elem('div', 'detail-title');
    var h2 = elem('h2', null, run.ticker || '-');
    titleRow.appendChild(h2);
    titleRow.appendChild(statusBadge(run.status));
    var dec = decisionBadge(run.decision);
    if (dec) titleRow.appendChild(dec);
    box.appendChild(titleRow);

    var grid = elem('div', 'detail-grid');

    function addItem(label, value) {
      var item = elem('div', 'detail-item');
      item.appendChild(elem('span', 'label', label));
      item.appendChild(elem('span', 'value', value));
      grid.appendChild(item);
    }

    addItem('분석 날짜', run.analysis_date || '-');
    addItem('분석 깊이', DEPTH_LABEL[run.depth] ? DEPTH_LABEL[run.depth] + ' (' + run.depth + ')' : String(run.depth || '-'));
    addItem('생성 시각', formatKST(run.created_at));
    addItem('갱신 시각', formatKST(run.updated_at));
    if (run.status === 'running' || run.status === 'queued') {
      var item = elem('div', 'detail-item');
      item.appendChild(elem('span', 'label', '경과 시간'));
      var val = elem('span', 'value run-elapsed', formatElapsed(run.created_at));
      val.dataset.created = run.created_at || '';
      item.appendChild(val);
      grid.appendChild(item);
    }
    addItem('실행 ID', run.run_id || '-');

    box.appendChild(grid);

    if (run.status === 'failed' && run.error) {
      var errBox = elem('div', 'detail-error', '오류: ' + run.error);
      box.appendChild(errBox);
    }
  }

  function renderReportNav(reports) {
    el.reportNav.textContent = '';
    reports.forEach(function (name) {
      var btn = elem('button', state.currentReport === name ? 'active' : '', reportLabel(name));
      btn.type = 'button';
      btn.addEventListener('click', function () {
        selectReport(state.route.runId, name);
      });
      btn.dataset.name = name;
      el.reportNav.appendChild(btn);
    });
  }

  function markNavActive(name) {
    Array.prototype.forEach.call(el.reportNav.children, function (btn) {
      btn.classList.toggle('active', btn.dataset.name === name);
    });
  }

  function selectReport(runId, name) {
    state.currentReport = name;
    markNavActive(name);
    el.reportContent.textContent = '';
    el.reportContent.appendChild(elem('p', 'report-loading', '보고서를 불러오는 중…'));

    apiFetch('/runs/' + encodeURIComponent(runId) + '/report?name=' + encodeURIComponent(name))
      .then(function (data) {
        if (state.route.runId !== runId || state.currentReport !== name) return;
        var md = (data && typeof data.content === 'string') ? data.content : '';
        // marked 렌더 결과만 innerHTML로 삽입
        el.reportContent.innerHTML = marked.parse(md);
      })
      .catch(function (err) {
        if (state.route.runId !== runId || state.currentReport !== name) return;
        el.reportContent.textContent = '';
        el.reportContent.appendChild(elem('p', 'report-error', '보고서를 불러오지 못했습니다: ' + err.message));
      });
  }

  function loadDetail(runId) {
    apiFetch('/runs/' + encodeURIComponent(runId)).then(function (data) {
      if (state.route.view !== 'detail' || state.route.runId !== runId) return;

      var run = data.run || {};
      renderDetailSummary(run);

      var reports = Array.isArray(data.reports) ? data.reports : [];
      if (reports.length > 0) {
        el.detailReports.hidden = false;
        renderReportNav(reports);
        if (!state.reportsLoaded) {
          state.reportsLoaded = true;
          var initial = reports.indexOf('complete_report.md') >= 0
            ? 'complete_report.md'
            : reports[0];
          selectReport(runId, initial);
        }
      } else {
        el.detailReports.hidden = true;
        var msg = run.status === 'failed'
          ? null
          : '분석이 완료되면 보고서가 표시됩니다.';
        if (msg) {
          var note = el.detailSummary.querySelector('.pending-note');
          if (!note) {
            note = elem('p', 'empty-msg pending-note', msg);
            el.detailSummary.appendChild(note);
          }
        }
      }

      if (run.status === 'queued' || run.status === 'running') {
        schedulePoll(function () { loadDetail(runId); });
      } else {
        stopPolling();
      }
    }).catch(function (err) {
      if (state.route.view !== 'detail' || state.route.runId !== runId) return;
      el.detailSummary.textContent = '';
      el.detailSummary.appendChild(elem('p', 'report-error', '실행 정보를 불러오지 못했습니다: ' + err.message));
      schedulePoll(function () { loadDetail(runId); });
    });
  }

  el.btnBack.addEventListener('click', function () {
    location.hash = '#/';
  });

  // ---------- 경과 시간 틱 ----------

  function startTicker() {
    if (state.tickTimer) return;
    state.tickTimer = setInterval(function () {
      var nodes = document.querySelectorAll('.run-elapsed');
      Array.prototype.forEach.call(nodes, function (node) {
        if (node.dataset.created) {
          node.textContent = formatElapsed(node.dataset.created);
        }
      });
    }, 1000);
  }

  // ---------- 라우팅 ----------

  function parseHash() {
    var hash = location.hash || '#/';
    var m = hash.match(/^#\/runs\/([^\/?#]+)/);
    if (m) {
      return { view: 'detail', runId: decodeURIComponent(m[1]) };
    }
    return { view: 'list', runId: null };
  }

  function applyRoute() {
    stopPolling();
    state.route = parseHash();

    if (state.route.view === 'detail') {
      el.viewList.hidden = true;
      el.viewDetail.hidden = false;
      state.currentReport = null;
      state.reportsLoaded = false;
      el.detailSummary.textContent = '';
      el.detailSummary.appendChild(elem('p', 'empty-msg', '불러오는 중…'));
      el.detailReports.hidden = true;
      el.reportNav.textContent = '';
      el.reportContent.textContent = '';
      loadDetail(state.route.runId);
    } else {
      el.viewDetail.hidden = true;
      el.viewList.hidden = false;
      el.runsList.textContent = '';
      el.runsList.appendChild(elem('p', 'empty-msg', '불러오는 중…'));
      loadRuns();
    }
    window.scrollTo(0, 0);
  }

  window.addEventListener('hashchange', applyRoute);

  // ---------- 초기화 ----------

  var today = todayStr();
  el.inputDate.value = today;
  el.inputDate.max = today;

  startTicker();
  applyRoute();
})();
