/* TradingAgents 웹 UI */
(function () {
  'use strict';

  var API_BASE = '/api';
  var POLL_INTERVAL = 10000;

  var CONFIG = window.APP_CONFIG || {};
  var COGNITO_URL = 'https://cognito-idp.' + (CONFIG.cognitoRegion || 'ap-northeast-2') + '.amazonaws.com/';
  var KEY_ACCESS = 'ta_access_token';
  var KEY_ID = 'ta_id_token';
  var KEY_REFRESH = 'ta_refresh_token';
  var KEY_EXPIRES = 'ta_token_expires';
  var EXPIRY_MARGIN_MS = 60000; // 만료 1분 전부터 갱신 시도

  // ---------- 상수 ----------

  var STATUS_LABEL = {
    queued: '대기중',
    running: '실행중',
    completed: '완료',
    failed: '실패',
    cancelled: '취소됨'
  };

  var STATUS_BADGE = {
    queued: 'badge-gray',
    running: 'badge-blue',
    completed: 'badge-green',
    failed: 'badge-red',
    cancelled: 'badge-gray'
  };

  // 진행 중 상태(취소 버튼 노출·폴링 지속 판단)
  var ACTIVE_STATUSES = { queued: true, running: true };

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

  var MARKET_LABEL = {
    KR: '한국',
    JP: '일본',
    US: '미국',
    CN: '중국'
  };

  // CNY는 엔화(¥)와 기호가 같아 혼동되므로 'CN¥'로 구분 표기한다.
  var CURRENCY_SYMBOL = {
    KRW: '₩',
    JPY: '¥',
    USD: '$',
    CNY: 'CN¥'
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
    reportContent: document.getElementById('report-content'),
    viewLogin: document.getElementById('view-login'),
    loginForm: document.getElementById('login-form'),
    loginEmail: document.getElementById('login-email'),
    loginPassword: document.getElementById('login-password'),
    btnLogin: document.getElementById('btn-login'),
    loginError: document.getElementById('login-error'),
    headerUser: document.getElementById('header-user'),
    userEmail: document.getElementById('user-email'),
    btnLogout: document.getElementById('btn-logout'),
    appNav: document.getElementById('app-nav'),
    navAnalysis: document.getElementById('nav-analysis'),
    navCatalog: document.getElementById('nav-catalog'),
    navAdmin: document.getElementById('nav-admin'),
    formSuggest: document.getElementById('form-suggest'),
    formSuggestList: document.getElementById('form-suggest-list'),
    viewCatalog: document.getElementById('view-catalog'),
    marketToggle: document.getElementById('market-toggle'),
    catalogSearch: document.getElementById('catalog-search'),
    catalogSector: document.getElementById('catalog-sector'),
    catalogSort: document.getElementById('catalog-sort'),
    catalogOrder: document.getElementById('catalog-order'),
    catalogStatus: document.getElementById('catalog-status'),
    catalogTableWrap: document.getElementById('catalog-table-wrap'),
    catalogBody: document.getElementById('catalog-body'),
    catalogEmpty: document.getElementById('catalog-empty'),
    catalogFooter: document.getElementById('catalog-footer'),
    catalogPrev: document.getElementById('catalog-prev'),
    catalogNext: document.getElementById('catalog-next'),
    catalogPageInfo: document.getElementById('catalog-pageinfo'),
    catalogGenerated: document.getElementById('catalog-generated'),
    viewAdmin: document.getElementById('view-admin'),
    adminDenied: document.getElementById('admin-denied'),
    adminContent: document.getElementById('admin-content'),
    adminConfigForm: document.getElementById('admin-config-form'),
    adminMaxRuns: document.getElementById('admin-max-runs'),
    btnSaveConfig: document.getElementById('btn-save-config'),
    adminConfigMsg: document.getElementById('admin-config-msg'),
    adminCreateForm: document.getElementById('admin-create-form'),
    adminNewEmail: document.getElementById('admin-new-email'),
    adminNewPassword: document.getElementById('admin-new-password'),
    adminNewIsAdmin: document.getElementById('admin-new-is-admin'),
    btnCreateUser: document.getElementById('btn-create-user'),
    adminCreateMsg: document.getElementById('admin-create-msg'),
    adminUsersStatus: document.getElementById('admin-users-status'),
    adminUsersBody: document.getElementById('admin-users-body'),
    toast: document.getElementById('toast')
  };

  var USER_STATUS_LABEL = {
    CONFIRMED: '정상',
    FORCE_CHANGE_PASSWORD: '비밀번호 변경 필요',
    RESET_REQUIRED: '재설정 필요',
    UNCONFIRMED: '미확인'
  };

  // ---------- 상태 ----------

  var state = {
    authed: false,
    route: { view: 'list', runId: null },
    pollTimer: null,
    tickTimer: null,
    currentReport: null,
    reportsLoaded: false
  };

  // 종목 탐색 화면 상태
  var catalog = {
    market: 'KR',
    q: '',
    sector: '',
    sort: 'name',
    order: 'asc',
    page: 1,
    pageSize: 50,
    reqId: 0,
    debounceTimer: null
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

  function formatNumber(value, decimals) {
    return Number(value).toLocaleString('ko-KR', {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals
    });
  }

  function formatPrice(price, currency) {
    if (price === null || price === undefined || isNaN(Number(price))) return '-';
    var cur = currency || '';
    var decimals = (cur === 'KRW' || cur === 'JPY') ? 0 : 2;
    var num = formatNumber(price, decimals);
    var sym = CURRENCY_SYMBOL[cur];
    if (sym) return sym + num;
    return cur ? num + ' ' + cur : num;
  }

  // 시총 축약 단위 계수: 100 이상은 정수, 그 미만은 소수 1자리
  function formatCapUnit(v) {
    if (v >= 100) return formatNumber(Math.round(v), 0);
    var s = v.toFixed(1);
    return s.slice(-2) === '.0' ? s.slice(0, -2) : s;
  }

  function formatMarketCap(cap, currency) {
    if (cap === null || cap === undefined || isNaN(Number(cap))) return '-';
    var v = Number(cap);
    if (v < 0) return '-';
    if (currency === 'USD') {
      if (v >= 1e9) return '$' + formatCapUnit(v / 1e9) + 'B';
      if (v >= 1e6) return '$' + formatCapUnit(v / 1e6) + 'M';
      return '$' + formatNumber(v, 0);
    }
    var sym = CURRENCY_SYMBOL[currency] || '';
    if (v >= 1e12) return sym + formatCapUnit(v / 1e12) + '조';
    if (v >= 1e8) return sym + formatCapUnit(v / 1e8) + '억';
    return sym + formatNumber(v, 0);
  }

  var toastTimer = null;

  function showToast(msg) {
    el.toast.textContent = msg;
    el.toast.hidden = false;
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      el.toast.hidden = true;
      toastTimer = null;
    }, 2500);
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

  // ---------- 인증 (Cognito) ----------

  function decodeJwtPayload(token) {
    try {
      var part = String(token).split('.')[1];
      var b64 = part.replace(/-/g, '+').replace(/_/g, '/');
      while (b64.length % 4 !== 0) b64 += '=';
      var raw = atob(b64);
      var bytes = new Uint8Array(raw.length);
      for (var i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
      return JSON.parse(new TextDecoder().decode(bytes));
    } catch (e) {
      return null;
    }
  }

  // 액세스 토큰의 cognito:groups에 admins가 포함되면 관리자.
  // (서버도 동일 클레임으로 admin 게이트를 검증하므로 UI 노출은 편의용)
  function isAdmin() {
    var payload = decodeJwtPayload(localStorage.getItem(KEY_ACCESS));
    if (!payload) return false;
    var groups = payload['cognito:groups'];
    return Array.isArray(groups) && groups.indexOf('admins') >= 0;
  }

  function saveTokens(result) {
    localStorage.setItem(KEY_ACCESS, result.AccessToken);
    if (result.IdToken) localStorage.setItem(KEY_ID, result.IdToken);
    if (result.RefreshToken) localStorage.setItem(KEY_REFRESH, result.RefreshToken);
    var expiresIn = typeof result.ExpiresIn === 'number' ? result.ExpiresIn : 3600;
    localStorage.setItem(KEY_EXPIRES, String(Date.now() + expiresIn * 1000));
  }

  function clearTokens() {
    localStorage.removeItem(KEY_ACCESS);
    localStorage.removeItem(KEY_ID);
    localStorage.removeItem(KEY_REFRESH);
    localStorage.removeItem(KEY_EXPIRES);
  }

  function hasTokens() {
    return !!(localStorage.getItem(KEY_ACCESS) && localStorage.getItem(KEY_REFRESH));
  }

  function isTokenExpired() {
    var expires = Number(localStorage.getItem(KEY_EXPIRES));
    if (!expires) return true;
    return Date.now() >= expires - EXPIRY_MARGIN_MS;
  }

  function cognitoRequest(target, payload) {
    return fetch(COGNITO_URL, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-amz-json-1.1',
        'X-Amz-Target': 'AWSCognitoIdentityProviderService.' + target
      },
      body: JSON.stringify(payload)
    }).then(function (res) {
      return res.text().then(function (text) {
        var data;
        try {
          data = JSON.parse(text);
        } catch (e) {
          data = {};
        }
        if (!res.ok) {
          var err = new Error(data.message || data.__type || ('인증 요청 실패 (HTTP ' + res.status + ')'));
          err.cognitoType = data.__type || '';
          throw err;
        }
        return data;
      });
    }, function () {
      throw new Error('네트워크 오류: 인증 서버에 연결할 수 없습니다.');
    });
  }

  function login(email, password) {
    return cognitoRequest('InitiateAuth', {
      AuthFlow: 'USER_PASSWORD_AUTH',
      ClientId: CONFIG.cognitoClientId,
      AuthParameters: { USERNAME: email, PASSWORD: password }
    }).then(function (data) {
      if (data.ChallengeName) {
        throw new Error('비밀번호 재설정이 필요한 계정입니다.');
      }
      if (!data.AuthenticationResult || !data.AuthenticationResult.AccessToken) {
        throw new Error('로그인 응답을 해석할 수 없습니다.');
      }
      saveTokens(data.AuthenticationResult);
    }, function (err) {
      if (err.cognitoType === 'NotAuthorizedException' ||
          err.cognitoType === 'UserNotFoundException') {
        throw new Error('이메일 또는 비밀번호가 올바르지 않습니다.');
      }
      throw err;
    });
  }

  var refreshPromise = null;

  function refreshTokens() {
    if (refreshPromise) return refreshPromise;
    var refreshToken = localStorage.getItem(KEY_REFRESH);
    if (!refreshToken) return Promise.reject(new Error('저장된 로그인 정보가 없습니다.'));
    refreshPromise = cognitoRequest('InitiateAuth', {
      AuthFlow: 'REFRESH_TOKEN_AUTH',
      ClientId: CONFIG.cognitoClientId,
      AuthParameters: { REFRESH_TOKEN: refreshToken }
    }).then(function (data) {
      var result = data.AuthenticationResult;
      if (!result || !result.AccessToken) {
        throw new Error('토큰 갱신 응답을 해석할 수 없습니다.');
      }
      saveTokens(result); // RefreshToken은 응답에 없으면 기존 값 유지
    }).finally(function () {
      refreshPromise = null;
    });
    return refreshPromise;
  }

  function handleAuthFailure() {
    clearTokens();
    showLogin();
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
      // 만료된 토큰이면 먼저 갱신 후 요청
      var ensure = isTokenExpired()
        ? refreshTokens().catch(function () {
            handleAuthFailure();
            throw new Error('로그인이 만료되었습니다. 다시 로그인해 주세요.');
          })
        : Promise.resolve();
      return ensure.then(function () {
        return doFetch(path, opts).catch(function (err) {
          if (!err || err.status !== 401) throw err;
          // 401: 토큰 갱신 1회 시도 후 재요청
          return refreshTokens().then(function () {
            return doFetch(path, opts);
          }, function () {
            handleAuthFailure();
            throw new Error('로그인이 만료되었습니다. 다시 로그인해 주세요.');
          }).catch(function (err2) {
            if (err2 && err2.status === 401) {
              handleAuthFailure();
              throw new Error('로그인이 만료되었습니다. 다시 로그인해 주세요.');
            }
            throw err2;
          });
        });
      });
    });
  }

  function doFetch(path, options) {
    options = options || {};
    var headers = {};
    if (options.headers) {
      Object.keys(options.headers).forEach(function (k) {
        headers[k] = options.headers[k];
      });
    }
    // Authorization 헤더는 CloudFront OAC 서명과 충돌하므로 커스텀 헤더 사용
    var access = localStorage.getItem(KEY_ACCESS);
    if (access) headers['x-access-token'] = access;

    var opts = { method: options.method || 'GET', headers: headers };
    if (options.body) opts.body = options.body;

    return fetch(API_BASE + path, opts).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        try {
          data = JSON.parse(text);
        } catch (e) {
          data = null;
        }
        if (!res.ok) {
          var msg = (data && data.error) ? data.error : ('서버 오류 (HTTP ' + res.status + ')');
          var err = new Error(msg);
          err.status = res.status;
          err.data = data;
          throw err;
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

  function clearSuggestions() {
    el.formSuggestList.textContent = '';
    el.formSuggest.hidden = true;
  }

  // POST /runs 400 응답의 suggestions로 "혹시 이 종목인가요?" 후보 버튼 표시
  function renderSuggestions(suggestions) {
    clearSuggestions();
    if (!Array.isArray(suggestions) || suggestions.length === 0) return;
    suggestions.forEach(function (s) {
      if (!s || !s.ticker) return;
      var label = s.name ? s.name + ' (' + s.ticker + ')' : String(s.ticker);
      var btn = elem('button', 'suggest-btn', label);
      btn.type = 'button';
      btn.addEventListener('click', function () {
        el.inputTicker.value = String(s.ticker).toUpperCase();
        setFormError(null);
        clearSuggestions();
        el.inputTicker.focus();
      });
      el.formSuggestList.appendChild(btn);
    });
    el.formSuggest.hidden = el.formSuggestList.children.length === 0;
  }

  el.form.addEventListener('submit', function (e) {
    e.preventDefault();
    setFormError(null);
    clearSuggestions();

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
      if (err.status === 400 && err.data) {
        renderSuggestions(err.data.suggestions);
      }
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

    // 진행 중이면 취소 버튼, 종료됐으면 재시작 패널
    if (ACTIVE_STATUSES[run.status]) {
      var cancel = buildCancelPanel(run);
      if (cancel) box.appendChild(cancel);
    } else {
      var restart = buildRestartPanel(run);
      if (restart) box.appendChild(restart);
    }
  }

  // 진행 중(queued/running)인 실행을 취소하는 버튼. Fargate 태스크를 중지하고
  // 상태를 취소됨으로 바꾼다. 확인 프롬프트 후 호출.
  function buildCancelPanel(run) {
    if (!run.run_id || !ACTIVE_STATUSES[run.status]) return null;
    var box = elem('div', 'cancel-box');
    var btn = elem('button', 'btn btn-danger', '실행 취소');
    var err = elem('p', 'restart-error');
    err.hidden = true;
    btn.addEventListener('click', function () {
      if (!window.confirm('이 분석 실행을 취소할까요? 진행 중인 작업이 중단됩니다.')) return;
      btn.disabled = true;
      btn.textContent = '취소 중…';
      err.hidden = true;
      apiFetch('/runs/' + encodeURIComponent(run.run_id) + '/cancel', { method: 'POST', body: '{}' })
        .then(function () { loadDetail(run.run_id); })
        .catch(function (e) {
          err.textContent = e.message;
          err.hidden = false;
          btn.disabled = false;
          btn.textContent = '실행 취소';
        });
    });
    box.appendChild(btn);
    box.appendChild(err);
    return box;
  }

  // 새 분석 폼과 동일한 깊이 옵션 (값, 레이블)
  var DEPTH_OPTIONS = [
    [1, '얕게 (빠름·저비용)'],
    [3, '중간 (균형)'],
    [5, '깊게 (느림·고비용)']
  ];

  // 완료/실패한 실행을 같은 종목·날짜로, 새 깊이로 재시작하는 패널.
  // 진행 중(queued/running)인 실행에는 표시하지 않는다.
  function buildRestartPanel(run) {
    if (!run.run_id) return null;
    if (run.status !== 'completed' && run.status !== 'failed' && run.status !== 'cancelled') return null;
    var failed = run.status === 'failed';

    var box = elem('div', 'restart-box' + (failed ? ' restart-box-failed' : ''));
    box.appendChild(elem('p', 'restart-title', failed ? '다른 깊이로 재시작' : '재시작'));
    box.appendChild(elem('p', 'restart-desc',
      (run.ticker || '-') + ' · ' + (run.analysis_date || '-') + '을(를) 새로운 분석 깊이로 다시 실행합니다.'));

    var origDepth = Number(run.depth) || 1;
    var origLabel = DEPTH_LABEL[origDepth] || origDepth;

    var row = elem('div', 'restart-row');

    var field = elem('div', 'restart-field');
    var selId = 'restart-depth-' + run.run_id;
    var label = elem('label', 'restart-label', '분석 깊이');
    label.setAttribute('for', selId);
    field.appendChild(label);

    var select = document.createElement('select');
    select.id = selId;
    select.className = 'restart-depth';
    DEPTH_OPTIONS.forEach(function (opt) {
      var o = elem('option', null, opt[1]);
      o.value = String(opt[0]);
      select.appendChild(o);
    });
    select.value = String(origDepth);
    field.appendChild(select);
    row.appendChild(field);

    var btn = elem('button', 'btn btn-primary restart-btn', '재시작');
    btn.type = 'button';
    row.appendChild(btn);
    box.appendChild(row);

    // 원본 대비 무엇이 바뀌는지(깊이) 안내
    var hint = elem('p', 'restart-hint');
    function updateHint() {
      var newDepth = parseInt(select.value, 10);
      var newLabel = DEPTH_LABEL[newDepth] || newDepth;
      hint.textContent = '원본 깊이: ' + origLabel + ' → 새 실행: ' + newLabel +
        (newDepth === origDepth ? ' (동일)' : '');
    }
    updateHint();
    select.addEventListener('change', updateHint);
    box.appendChild(hint);

    var err = elem('p', 'restart-error');
    err.hidden = true;
    box.appendChild(err);

    btn.addEventListener('click', function () {
      var newDepth = parseInt(select.value, 10);
      err.hidden = true;
      btn.disabled = true;
      select.disabled = true;
      btn.textContent = '요청 중…';
      apiFetch('/runs/' + encodeURIComponent(run.run_id) + '/restart', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ depth: newDepth })
      }).then(function (data) {
        if (data && data.run_id) {
          location.hash = '#/runs/' + encodeURIComponent(data.run_id);
        }
      }).catch(function (e) {
        err.textContent = e.message;
        err.hidden = false;
        btn.disabled = false;
        select.disabled = false;
        btn.textContent = '재시작';
      });
    });

    return box;
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

  // ---------- 종목 탐색 화면 ----------

  function setCatalogLoading(loading) {
    if (loading) {
      el.catalogStatus.textContent = '';
      el.catalogStatus.appendChild(elem('span', 'spinner'));
      el.catalogStatus.appendChild(document.createTextNode(' 불러오는 중…'));
      el.catalogStatus.hidden = false;
    } else {
      el.catalogStatus.textContent = '';
      el.catalogStatus.hidden = true;
    }
  }

  function setCatalogEmpty(msg) {
    if (msg) {
      el.catalogEmpty.textContent = msg;
      el.catalogEmpty.hidden = false;
      el.catalogTableWrap.hidden = true;
      el.catalogFooter.hidden = true;
    } else {
      el.catalogEmpty.textContent = '';
      el.catalogEmpty.hidden = true;
      el.catalogTableWrap.hidden = false;
    }
  }

  // 응답의 sectors로 업종 드롭다운 채움 (현재 선택 유지)
  function fillSectors(sectors) {
    if (!Array.isArray(sectors)) return;
    var current = el.catalogSector.value;
    el.catalogSector.textContent = '';
    var optAll = elem('option', null, '전체 업종');
    optAll.value = '';
    el.catalogSector.appendChild(optAll);
    sectors.forEach(function (s) {
      if (!s) return;
      var opt = elem('option', null, String(s));
      opt.value = String(s);
      el.catalogSector.appendChild(opt);
    });
    el.catalogSector.value = current;
    if (el.catalogSector.value !== current) {
      // 목록에 없는 업종이면 전체로 되돌림
      el.catalogSector.value = '';
      catalog.sector = '';
    }
  }

  // 카탈로그 행 클릭 → 분석 화면으로 전환하며 티커 자동 입력
  function pickTickerForAnalysis(ticker) {
    var t = String(ticker).toUpperCase();
    el.inputTicker.value = t;
    setFormError(null);
    clearSuggestions();
    location.hash = '#/';
    // hashchange로 화면 전환이 끝난 뒤 강조 표시
    setTimeout(function () {
      el.inputTicker.focus();
      el.inputTicker.classList.add('input-flash');
      setTimeout(function () {
        el.inputTicker.classList.remove('input-flash');
      }, 1500);
    }, 50);
    showToast('종목 코드 ' + t + '을(를) 분석 폼에 입력했습니다.');
  }

  function renderCatalog(data) {
    var items = Array.isArray(data.items) ? data.items : [];
    fillSectors(data.sectors);

    el.catalogBody.textContent = '';
    if (items.length === 0) {
      setCatalogEmpty('조건에 맞는 종목이 없습니다.');
    } else {
      setCatalogEmpty(null);
      items.forEach(function (item) {
        var tr = elem('tr', 'catalog-row');
        tr.setAttribute('role', 'button');
        tr.tabIndex = 0;

        tr.appendChild(elem('td', 'cat-name', item.name || '-'));
        tr.appendChild(elem('td', 'cat-ticker', item.ticker || '-'));
        tr.appendChild(elem('td', 'cat-market', MARKET_LABEL[item.market] || item.market || '-'));
        tr.appendChild(elem('td', 'cat-sector', item.sector || '-'));
        tr.appendChild(elem('td', 'num', formatPrice(item.price, item.currency)));
        tr.appendChild(elem('td', 'num', formatMarketCap(item.market_cap, item.currency)));

        function pick() {
          if (item.ticker) pickTickerForAnalysis(item.ticker);
        }
        tr.addEventListener('click', pick);
        tr.addEventListener('keydown', function (e) {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            pick();
          }
        });

        el.catalogBody.appendChild(tr);
      });
    }

    var total = Number(data.total) || 0;
    catalog.pageSize = Number(data.page_size) || 50;
    var pages = Math.max(1, Math.ceil(total / catalog.pageSize));
    if (catalog.page > pages) catalog.page = pages;

    el.catalogPageInfo.textContent =
      catalog.page + ' / ' + pages + ' 페이지 · 전체 ' + formatNumber(total, 0) + '건';
    el.catalogPrev.disabled = catalog.page <= 1;
    el.catalogNext.disabled = catalog.page >= pages;
    el.catalogGenerated.textContent = data.generated_at
      ? '데이터 기준: ' + formatKST(data.generated_at) + ' KST'
      : '';
    el.catalogFooter.hidden = false;
  }

  function loadCatalog() {
    var reqId = ++catalog.reqId;
    setCatalogLoading(true);

    var params = [
      'market=' + encodeURIComponent(catalog.market),
      'sort=' + encodeURIComponent(catalog.sort),
      'order=' + encodeURIComponent(catalog.order),
      'page=' + encodeURIComponent(catalog.page)
    ];
    if (catalog.q) params.push('q=' + encodeURIComponent(catalog.q));
    if (catalog.sector) params.push('sector=' + encodeURIComponent(catalog.sector));

    apiFetch('/catalog?' + params.join('&')).then(function (data) {
      if (state.route.view !== 'catalog' || reqId !== catalog.reqId) return;
      setCatalogLoading(false);
      renderCatalog(data);
    }).catch(function (err) {
      if (state.route.view !== 'catalog' || reqId !== catalog.reqId) return;
      setCatalogLoading(false);
      el.catalogBody.textContent = '';
      if (err.status === 404) {
        setCatalogEmpty('종목 카탈로그가 아직 생성되지 않았습니다. 일 1회 배치로 생성될 예정이니 잠시 후 다시 확인해 주세요.');
      } else {
        setCatalogEmpty('종목 목록을 불러오지 못했습니다: ' + err.message);
      }
    });
  }

  function catalogReload(resetPage) {
    if (resetPage) catalog.page = 1;
    loadCatalog();
  }

  el.marketToggle.addEventListener('click', function (e) {
    var btn = e.target.closest('button[data-market]');
    if (!btn) return;
    var market = btn.dataset.market;
    if (market === catalog.market) return;
    catalog.market = market;
    catalog.sector = '';
    el.catalogSector.value = '';
    Array.prototype.forEach.call(
      el.marketToggle.querySelectorAll('button[data-market]'),
      function (b) { b.classList.toggle('active', b.dataset.market === market); }
    );
    catalogReload(true);
  });

  el.catalogSearch.addEventListener('input', function () {
    if (catalog.debounceTimer) clearTimeout(catalog.debounceTimer);
    catalog.debounceTimer = setTimeout(function () {
      catalog.debounceTimer = null;
      var q = el.catalogSearch.value.trim();
      if (q === catalog.q) return;
      catalog.q = q;
      catalogReload(true);
    }, 400);
  });

  el.catalogSector.addEventListener('change', function () {
    catalog.sector = el.catalogSector.value;
    catalogReload(true);
  });

  el.catalogSort.addEventListener('change', function () {
    catalog.sort = el.catalogSort.value;
    catalogReload(true);
  });

  el.catalogOrder.addEventListener('change', function () {
    catalog.order = el.catalogOrder.value;
    catalogReload(true);
  });

  el.catalogPrev.addEventListener('click', function () {
    if (catalog.page <= 1) return;
    catalog.page -= 1;
    loadCatalog();
  });

  el.catalogNext.addEventListener('click', function () {
    catalog.page += 1;
    loadCatalog();
  });

  // ---------- 관리자 화면 ----------

  function setAdminMsg(node, msg, isError) {
    if (msg) {
      node.textContent = msg;
      node.classList.toggle('admin-msg-error', !!isError);
      node.hidden = false;
    } else {
      node.textContent = '';
      node.hidden = true;
    }
  }

  function loadAdmin() {
    setAdminMsg(el.adminConfigMsg, null);
    setAdminMsg(el.adminCreateMsg, null);
    loadAdminConfig();
    loadAdminUsers();
  }

  function loadAdminConfig() {
    el.adminMaxRuns.value = '';
    apiFetch('/admin/config').then(function (data) {
      if (state.route.view !== 'admin') return;
      el.adminMaxRuns.value = data.max_active_runs;
    }).catch(function (err) {
      if (state.route.view !== 'admin') return;
      setAdminMsg(el.adminConfigMsg, '현재 값을 불러오지 못했습니다: ' + err.message, true);
    });
  }

  el.adminConfigForm.addEventListener('submit', function (e) {
    e.preventDefault();
    var val = parseInt(el.adminMaxRuns.value, 10);
    if (isNaN(val) || val < 1 || val > 50) {
      setAdminMsg(el.adminConfigMsg, '1~50 사이의 숫자를 입력해 주세요.', true);
      return;
    }
    el.btnSaveConfig.disabled = true;
    apiFetch('/admin/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ max_active_runs: val })
    }).then(function (data) {
      el.adminMaxRuns.value = data.max_active_runs;
      setAdminMsg(el.adminConfigMsg, '저장되었습니다. (현재 ' + data.max_active_runs + '건)', false);
    }).catch(function (err) {
      setAdminMsg(el.adminConfigMsg, '저장 실패: ' + err.message, true);
    }).finally(function () {
      el.btnSaveConfig.disabled = false;
    });
  });

  function renderAdminUsers(users) {
    el.adminUsersBody.textContent = '';
    if (!Array.isArray(users) || users.length === 0) {
      var trEmpty = elem('tr');
      var tdEmpty = elem('td', 'empty-msg', '사용자가 없습니다.');
      tdEmpty.colSpan = 4;
      trEmpty.appendChild(tdEmpty);
      el.adminUsersBody.appendChild(trEmpty);
      return;
    }
    users.forEach(function (u) {
      var tr = elem('tr');
      tr.appendChild(elem('td', 'admin-user-email', u.email || u.username || '-'));

      var statusLabel = u.enabled === false
        ? '비활성'
        : (USER_STATUS_LABEL[u.status] || u.status || '-');
      tr.appendChild(elem('td', 'admin-user-status', statusLabel));

      var adminTd = elem('td');
      adminTd.appendChild(elem('span',
        'badge ' + (u.is_admin ? 'badge-green' : 'badge-gray'),
        u.is_admin ? '관리자' : '일반'));
      tr.appendChild(adminTd);

      var actionsTd = elem('td', 'admin-actions');

      var btnPw = elem('button', 'btn btn-ghost btn-sm', '비밀번호');
      btnPw.type = 'button';
      btnPw.addEventListener('click', function () { resetUserPassword(u); });
      actionsTd.appendChild(btnPw);

      var btnAdmin = elem('button', 'btn btn-ghost btn-sm',
        u.is_admin ? '관리자 해제' : '관리자 지정');
      btnAdmin.type = 'button';
      btnAdmin.addEventListener('click', function () { toggleUserAdmin(u); });
      actionsTd.appendChild(btnAdmin);

      var btnDel = elem('button', 'btn btn-ghost btn-sm btn-danger', '삭제');
      btnDel.type = 'button';
      btnDel.addEventListener('click', function () { deleteUser(u); });
      actionsTd.appendChild(btnDel);

      tr.appendChild(actionsTd);
      el.adminUsersBody.appendChild(tr);
    });
  }

  function loadAdminUsers() {
    el.adminUsersStatus.textContent = '불러오는 중…';
    apiFetch('/admin/users').then(function (data) {
      if (state.route.view !== 'admin') return;
      renderAdminUsers(data.users || []);
      el.adminUsersStatus.textContent = '총 ' + ((data.users || []).length) + '명';
    }).catch(function (err) {
      if (state.route.view !== 'admin') return;
      el.adminUsersStatus.textContent = '불러오기 실패: ' + err.message;
    });
  }

  function resetUserPassword(u) {
    var who = u.email || u.username;
    var pw = window.prompt(who + '의 새 비밀번호를 입력하세요 (8자 이상):');
    if (pw === null) return;
    if (pw.length < 8) {
      showToast('비밀번호는 8자 이상이어야 합니다.');
      return;
    }
    apiFetch('/admin/users/' + encodeURIComponent(u.username) + '/reset-password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: pw })
    }).then(function () {
      showToast(who + '의 비밀번호를 재설정했습니다.');
    }).catch(function (err) {
      showToast('실패: ' + err.message);
    });
  }

  function toggleUserAdmin(u) {
    var who = u.email || u.username;
    var next = !u.is_admin;
    var msg = next ? '관리자 권한을 부여할까요?' : '관리자 권한을 해제할까요?';
    if (!window.confirm(who + '\n' + msg)) return;
    apiFetch('/admin/users/' + encodeURIComponent(u.username) + '/admin', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ is_admin: next })
    }).then(function () {
      showToast('변경했습니다.');
      loadAdminUsers();
    }).catch(function (err) {
      showToast('실패: ' + err.message);
    });
  }

  function deleteUser(u) {
    var who = u.email || u.username;
    if (!window.confirm(who + ' 계정을 삭제할까요?\n되돌릴 수 없습니다.')) return;
    apiFetch('/admin/users/' + encodeURIComponent(u.username), {
      method: 'DELETE'
    }).then(function () {
      showToast(who + ' 계정을 삭제했습니다.');
      loadAdminUsers();
    }).catch(function (err) {
      showToast('실패: ' + err.message);
    });
  }

  el.adminCreateForm.addEventListener('submit', function (e) {
    e.preventDefault();
    var email = el.adminNewEmail.value.trim().toLowerCase();
    var pw = el.adminNewPassword.value;
    var isAdm = el.adminNewIsAdmin.checked;
    if (!email) {
      setAdminMsg(el.adminCreateMsg, '이메일을 입력해 주세요.', true);
      return;
    }
    if (pw.length < 8) {
      setAdminMsg(el.adminCreateMsg, '비밀번호는 8자 이상이어야 합니다.', true);
      return;
    }
    el.btnCreateUser.disabled = true;
    apiFetch('/admin/users', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: email, password: pw, is_admin: isAdm })
    }).then(function () {
      el.adminNewEmail.value = '';
      el.adminNewPassword.value = '';
      el.adminNewIsAdmin.checked = false;
      setAdminMsg(el.adminCreateMsg, email + ' 계정을 추가했습니다.', false);
      loadAdminUsers();
    }).catch(function (err) {
      setAdminMsg(el.adminCreateMsg, '추가 실패: ' + err.message, true);
    }).finally(function () {
      el.btnCreateUser.disabled = false;
    });
  });

  // ---------- 라우팅 ----------

  function parseHash() {
    var hash = location.hash || '#/';
    var m = hash.match(/^#\/runs\/([^\/?#]+)/);
    if (m) {
      return { view: 'detail', runId: decodeURIComponent(m[1]) };
    }
    if (/^#\/catalog(?:[\/?#]|$)/.test(hash)) {
      return { view: 'catalog', runId: null };
    }
    if (/^#\/admin(?:[\/?#]|$)/.test(hash)) {
      return { view: 'admin', runId: null };
    }
    return { view: 'list', runId: null };
  }

  function updateNav() {
    el.navAnalysis.classList.toggle('active',
      state.route.view === 'list' || state.route.view === 'detail');
    el.navCatalog.classList.toggle('active', state.route.view === 'catalog');
    el.navAdmin.classList.toggle('active', state.route.view === 'admin');
  }

  function applyRoute() {
    if (!state.authed) return;
    stopPolling();
    state.route = parseHash();
    updateNav();

    if (state.route.view === 'detail') {
      el.viewList.hidden = true;
      el.viewCatalog.hidden = true;
      el.viewAdmin.hidden = true;
      el.viewDetail.hidden = false;
      state.currentReport = null;
      state.reportsLoaded = false;
      el.detailSummary.textContent = '';
      el.detailSummary.appendChild(elem('p', 'empty-msg', '불러오는 중…'));
      el.detailReports.hidden = true;
      el.reportNav.textContent = '';
      el.reportContent.textContent = '';
      loadDetail(state.route.runId);
    } else if (state.route.view === 'catalog') {
      el.viewList.hidden = true;
      el.viewDetail.hidden = true;
      el.viewAdmin.hidden = true;
      el.viewCatalog.hidden = false;
      loadCatalog();
    } else if (state.route.view === 'admin') {
      el.viewList.hidden = true;
      el.viewDetail.hidden = true;
      el.viewCatalog.hidden = true;
      el.viewAdmin.hidden = false;
      if (isAdmin()) {
        el.adminDenied.hidden = true;
        el.adminContent.hidden = false;
        loadAdmin();
      } else {
        // 비관리자가 #/admin에 직접 접근한 경우
        el.adminDenied.hidden = false;
        el.adminContent.hidden = true;
      }
    } else {
      el.viewDetail.hidden = true;
      el.viewCatalog.hidden = true;
      el.viewAdmin.hidden = true;
      el.viewList.hidden = false;
      el.runsList.textContent = '';
      el.runsList.appendChild(elem('p', 'empty-msg', '불러오는 중…'));
      loadRuns();
    }
    window.scrollTo(0, 0);
  }

  window.addEventListener('hashchange', applyRoute);

  // ---------- 로그인 화면 ----------

  function setLoginError(msg) {
    if (msg) {
      el.loginError.textContent = msg;
      el.loginError.hidden = false;
    } else {
      el.loginError.textContent = '';
      el.loginError.hidden = true;
    }
  }

  function showLogin() {
    state.authed = false;
    stopPolling();
    el.viewList.hidden = true;
    el.viewDetail.hidden = true;
    el.viewCatalog.hidden = true;
    el.viewAdmin.hidden = true;
    el.appNav.hidden = true;
    el.navAdmin.hidden = true;
    el.headerUser.hidden = true;
    el.userEmail.textContent = '';
    el.viewLogin.hidden = false;
    el.loginPassword.value = '';
    el.loginEmail.focus();
  }

  function showApp() {
    state.authed = true;
    setLoginError(null);
    el.viewLogin.hidden = true;

    var payload = decodeJwtPayload(localStorage.getItem(KEY_ID));
    el.userEmail.textContent = (payload && payload.email) ? payload.email : '';
    el.headerUser.hidden = false;
    el.appNav.hidden = false;
    // 관리자 탭은 admins 그룹 소속일 때만 노출
    el.navAdmin.hidden = !isAdmin();

    applyRoute();
  }

  el.loginForm.addEventListener('submit', function (e) {
    e.preventDefault();
    setLoginError(null);

    var email = el.loginEmail.value.trim();
    var password = el.loginPassword.value;
    if (!email || !password) {
      setLoginError('이메일과 비밀번호를 입력해 주세요.');
      return;
    }

    el.btnLogin.disabled = true;
    el.btnLogin.textContent = '로그인 중…';

    login(email, password).then(function () {
      el.loginPassword.value = '';
      showApp();
    }).catch(function (err) {
      setLoginError(err.message);
    }).finally(function () {
      el.btnLogin.disabled = false;
      el.btnLogin.textContent = '로그인';
    });
  });

  el.btnLogout.addEventListener('click', function () {
    clearTokens();
    showLogin();
  });

  // ---------- 초기화 ----------

  var today = todayStr();
  el.inputDate.value = today;
  el.inputDate.max = today;

  startTicker();

  if (hasTokens()) {
    // 토큰 만료/무효 여부는 첫 API 호출에서 갱신 시도로 처리된다.
    showApp();
  } else {
    clearTokens();
    showLogin();
  }
})();
