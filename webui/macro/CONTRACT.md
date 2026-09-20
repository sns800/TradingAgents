# G20 매크로 데이터 계약 (CONTRACT) — 모든 모듈이 따르는 단일 기준

> 이 문서는 수집기(`webui/macro/*`), Lambda API(`webui/backend/macro_api.py`), 프론트(`webui/frontend/macro.js`),
> 인프라(`webui/infra/template.yaml`)가 공유하는 **키·스키마·응답 형식**을 고정한다. 바꾸려면 이 문서를 먼저 고친다.
> 배경·설계 근거: [../G20-매크로-대시보드-제안.md](../G20-매크로-대시보드-제안.md)

## 0. 사용자 결정 (2026-09-20 확정)
- 정치(여론조사) 대상: **17개국 전부** (중국·사우디·유로존은 경쟁 정당 없음 → `not_applicable` 문서로 표기)
- 저장소: **DynamoDB 단일 테이블** `tradingagents-webui-macro` (+ S3 원본 보존). Parquet/Athena는 Phase 3.
- 배치: **기존 CloudFormation 스택** `tradingagents-webui`에 리소스 추가. 워커 이미지는 공용.
- AI 판정: 검토 전에도 **노출 + `ai_generated` 배지** (`review_status: pending|approved|rejected`)
- 금액: **USD 통일**, 한국만 원화 병기(프론트 표시 규칙)

## 1. 대상 국가 (20 = 19개국 + 유로존)
`iso`(ISO 3166-1 alpha-2)가 시스템 전체의 국가 키. 유로존은 `EU`.

| iso | iso3 | name_ko | ccy | groups | euro | 비고 |
|---|---|---|---|---|---|---|
| KR | KOR | 한국 | KRW | G20,ASIA | no | |
| US | USA | 미국 | USD | G20,G7 | no | 환율 = 1 (기준통화) |
| JP | JPN | 일본 | JPY | G20,G7,ASIA | no | |
| CN | CHN | 중국 | CNY | G20,BRICS,ASIA | no | 정당 경쟁 없음 |
| EU | EA20 | 유로존 | EUR | G20,EU | — | BIS `XM`, WB `EMU`(응답 매칭은 `countryiso3code=EMU`), IMF `U2`(MFS_MA 미수록), OECD `EA20` — 단 `DSD_G20_PRICES@DF_G20_PRICES`에서는 **`EA`**, Ember는 API 집계 엔티티 `entity=EU`(`entity_code` 옵션엔 없음)·공개 CSV Area `EU`, OWID는 유로존 행 없음 |
| DE | DEU | 독일 | EUR | G20,G7,EU | yes | 금리·통화량·환율은 EU 참조 |
| FR | FRA | 프랑스 | EUR | G20,G7,EU | yes | 〃 |
| IT | ITA | 이탈리아 | EUR | G20,G7,EU | yes | 〃 |
| GB | GBR | 영국 | GBP | G20,G7 | no | |
| CA | CAN | 캐나다 | CAD | G20,G7 | no | |
| AU | AUS | 호주 | AUD | G20,ASIA | no | |
| IN | IND | 인도 | INR | G20,BRICS,ASIA | no | |
| ID | IDN | 인도네시아 | IDR | G20,ASIA | no | |
| BR | BRA | 브라질 | BRL | G20,BRICS | no | |
| MX | MEX | 멕시코 | MXN | G20 | no | |
| AR | ARG | 아르헨티나 | ARS | G20 | no | |
| TR | TUR | 튀르키예 | TRY | G20 | no | |
| SA | SAU | 사우디아라비아 | SAR | G20 | no | 정당 없음 |
| ZA | ZAF | 남아프리카공화국 | ZAR | G20,BRICS | no | |
| RU | RUS | 러시아 | RUB | G20,BRICS | no | 조사 신뢰도 낮음 |

`euro: yes` 국가의 `policy_rate`, `m2_*`, `fx_*`는 수집하지 않고 API/프론트가 `EU` 값을 참조한다(`flags: [euro_area_shared]`).

## 2. 지표 레지스트리 (`webui/macro/registry.yaml`)
```yaml
countries: [...]            # 1장의 표 + 소스별 코드(bis, oecd, wb, imf, yahoo_fx, ember, owid)
indicators:
  - id: policy_rate         # 시스템 전체 지표 키 (snake_case, 아래 목록 고정)
    name_ko: 정책금리
    unit: "%"               # "%", "index", "usd", "usd_bn", "lcu_bn", "lcu_per_usd", "pct_gdp", "pct_share", "score"
    category: monetary      # politics | monetary | fx | structure | energy | fiscal | market
    native_freq: D          # D | M | Q | Y | W(여론조사) | E(이벤트)
    store_freqs: [D, M, Q, Y]   # 저장하는 빈도 (native 이하만)
    agg: last               # 저빈도 집계: last(기말) | mean | sum | none(연간 원천만)
    yoy_from: null          # 전년비 파생의 원천 지표 id (예: cpi_yoy.yoy_from = cpi_index)
    decimals: 2
    higher_is: neutral      # 색 판단 금지. 항상 neutral (문서화 목적)
    coverage_note: "16/20 — AR 없음(BIS 미제공)"   # 선택. 소스 실측 커버리지 메모(자유 문자열)
    sources:                # 우선순위 순. 실패 시 다음 소스
      - { name: bis, series: "WS_CBPOL/1.0/D.{bis}", priority: 1 }
      - { name: fred, series: "FEDFUNDS", only: [US], freq: M, priority: 2 }
```
**unit 의미** — `%` · `index`(지수) · `usd`(USD **원단위**. World Bank 원값 그대로) · `usd_bn`(**10억 USD**) ·
`lcu_bn`(**자국통화 10억 단위 잔액**. `m2_level` 전용 — USD 환산을 하지 않으므로 국가 간 절대값 비교 금지) ·
`lcu_per_usd`(현지통화/USD) · `pct_gdp` · `pct_share` · `score`.
소스 항목에 `unit`/`freq`를 적으면 **그 소스가 만드는 관측치만** 단위·빈도가 다르다는 뜻이다
(예: `m2_level`의 FRED `M2SL`은 `usd_bn`, World Bank 폴백은 `freq: Y`).
`coverage_note`는 소스 모듈 실측 커버리지 메모이며 `registry.py`의 `Indicator.extra`로 흡수된다(API 메타로는 나가지 않음).

**지표 id 고정 목록** (부록 A와 동일):
`policy_rate, fx_usd, fx_value_index, m2_level, m2_yoy, cpi_index, cpi_yoy, core_cpi_yoy, ppi_index, ppi_yoy, house_price_index, house_price_yoy, gdp_usd, gdp_growth, gni_usd, gni_pc, gov_expense_gdp, gov_revenue_gdp, gov_debt_gdp, mil_gdp, mil_expenditure_share, mil_usd, va_agri, va_industry, va_manuf, va_services, exports_gdp, exports_top_hs2, elec_mix, energy_import_dep, fuel_dep_oil, fuel_dep_gas, fuel_dep_coal, party_support, gov_approval, cb_stance, top_companies, dxy`
- `elec_mix`, `exports_top_hs2`, `top_companies`는 **복합값**(JSON 목록)이며 `value`가 아니라 `payload`에 저장 (아래 3장).

## 3. 관측치(Observation) 스키마 — `webui/macro/schema.py`의 dataclass와 1:1
| 필드 | 타입 | 규칙 |
|---|---|---|
| `indicator` | str | 2장 목록 |
| `iso` | str | 1장 |
| `freq` | `D|M|Q|Y|W|E` | |
| `period` | str | D `YYYY-MM-DD` · W `YYYY-MM-DD`(조사 종료일) · M `YYYY-MM` · Q `YYYY-Qn` · Y `YYYY` · E `YYYY-MM-DD` |
| `value` | float \| None | 복합값이면 None |
| `payload` | dict \| None | 복합값(예: `{"items":[{"label":"석탄","value":29.1}]}`) |
| `unit` | str | 2장 unit 값 |
| `source` | str | **실제 원천**: `bis, oecd, worldbank, imf, fred, ember, owid, wits, yahoo, catalog, wiki_polls, cb_statements, derived` · 레지스트리·INGEST의 **모듈 식별자**로는 `companies`(top_companies)도 쓴다 — 이 모듈이 만든 관측치의 `source`는 경로에 따라 `catalog`(KR/JP/US/CN) 또는 `yahoo`(나머지)다. `fuel_dep_*`는 레지스트리상 `derived via owid`지만 관측치 `source`는 `owid` |
| `series_id` | str | 소스 내 시리즈 식별자 (예: `WS_CBPOL/1.0/D.KR`, `NY.GDP.MKTP.CD`) |
| `source_url` | str | 사람이 열 수 있는 URL |
| `retrieved_at` | str ISO8601 UTC | 수집 시각 |
| `vintage` | str `YYYY-MM-DD` | 소스가 값을 발표/갱신한 날(모르면 retrieved 날짜) |
| `method` | str | 집계 설명 한국어 (예: `월말 기준값 (원천: 일별)`) |
| `flags` | list[str] | `euro_area_shared, estimated, derived, ai_generated, needs_review, fallback_source, partial_period` |

**복합값 `payload` 규칙** (`elec_mix`, `exports_top_hs2`, `top_companies` — `value`는 항상 None)
- `payload.items[]`는 **항상 `label`(표시 이름) + `value` 쌍**을 갖고(`value`는 비중 % — `top_companies`만
  시총 USD 10억 = `market_cap_usd_bn`), 여기에 타입별 필드를 덧붙인다.
  프론트는 `label`/`value`만으로 세 지표를 같은 규칙으로 렌더할 수 있어야 한다. `value` 내림차순 정렬(없으면 뒤로).
- `elec_mix`: item에 `twh` 추가 · payload에 `total_twh`
- `exports_top_hs2`: item에 `hs2`(HS 챕터 **구간** 문자열, 예 `84-85`), `label_ko`, `label_en`, `usd_mn` 추가 ·
  payload에 `total_usd_mn`(+`n_groups`)
- `top_companies`: item에 `name`, `ticker`, `sector`, `market_cap_usd_bn`, `market_cap_local`, `currency` 추가 ·
  payload에 `asof`, `source_detail`

**DynamoDB 저장 시** float → `Decimal(str(round(v, 6)))`. (put_item에 float 금지)

## 4. DynamoDB 키 설계 — 테이블 `tradingagents-webui-macro` (PK `pk`, SK `sk`, 온디맨드)
| pk | sk | 속성 | 용도 |
|---|---|---|---|
| `SERIES#<indicator>` | `META` | registry의 indicator 항목 + `updated_at` | 지표 사전 |
| `OBS#<indicator>#<iso>` | `<freq>#<period>` (예 `M#2026-08`, `Q#2026-Q2`, `Y#2025`, `D#2026-09-19`) | 3장 필드 전부 + `revisions: [{value, vintage, retrieved_at}]`(최대 10, 값이 바뀔 때만 추가) + `updated_at` | 시계열 Query (`begins_with(sk, "M#")` + between) |
| `LATEST#<indicator>` | `<iso>` | `freq, period, value, payload, unit, prev_value, prev_period, change, change_pct, rank, n, updated_at, flags, source` | 개요 히트맵 (지표당 Query 1회). `rank`는 value 내림차순 1부터 |
| `SNAPSHOT#<iso>` | `PROFILE` | `latest: {indicator: {...LATEST 속성...}}, docs: {cb_stance: <최신 DOC 요약>, energy_policy: ..., election: ..., polls: {...}}, updated_at` | 국가 프로필 Get 1회 |
| `DOC#<type>#<iso>` | `<date>#<id>` (`date`=`YYYY-MM-DD`, `id`=12자리 hex) | 5장 문서 스키마 | 정성 문서 |
| `INGEST#<source>` | `<run_ts ISO8601>` | `status: ok|partial|failed, started_at, finished_at, n_obs, n_docs, errors: [..], countries_ok, countries_failed` | 수집 로그 |
| `INGEST#<source>` | `LATEST` | 위 요약 복사 + `next_due` (+ 실패 시 `retry_reason`) | 케이던스 판단 (규칙은 12장) |
| `CONFIG` | `MACRO` | `llm_daily_token_budget, llm_tokens_used_today, llm_tokens_date` | LLM 예산. env `MACRO_LLM_DAILY_TOKEN_BUDGET`는 이 항목이 **없을 때 초기값**으로만 쓰고, 있으면 CONFIG가 우선 |
| `CONFIG` | `LOCK#collect` | `holder(run_ts), acquired_at, expires_at(획득 + 3시간), updated_at` | 수집기 **동시 실행 방지 락**. collect.py가 시작 시 `attribute_not_exists(pk) OR expires_at < :now` 조건부 put으로 획득, 종료(finally)에 `holder` 일치 조건으로 삭제. 획득 실패면 로그 후 exit 0. `POST /api/admin/macro/refresh`는 만료 전 락이 있으면 409 |

역인덱스(GSI) 없음. 국가 프로필은 SNAPSHOT 1건, 개요는 LATEST N Query, 시계열은 OBS Query.

## 5. 문서(Doc) 스키마 — `DOC#<type>#<iso>`
공통: `type, iso, date, id, title_ko, summary_ko, source_url, source_name, retrieved_at, ai_generated(bool), model_id, confidence(0..1), quotes: [str](AI면 필수·1개 이상), review_status: pending|approved|rejected, reviewed_by, reviewed_at, s3_key(전문), payload`

| type | payload |
|---|---|
| `cb_stance` | `{stance_score: -2..2 (0.5 단위), direction: hike|hold|cut, forward_guidance: tightening|neutral|easing, rate_after: float|null, statement_date, meeting_type}` |
| `poll` | `{pollster, fieldwork_start, fieldwork_end, sample_size: int|null, results: {"<정당명>": pct}, gov_approval: pct|null, method: str|null}` — `date`=fieldwork_end |
| `poll_of_polls` | `{asof, window_days: 30, results: {"<정당명>": pct}, n_polls, ruling_party, ruling_pct, leader_party, leader_pct, gov_approval}` — 서버 계산, `ai_generated=false` |
| `election` | `{next_election_date, election_type, ruling_party, ruling_lean, second_party, system_note}` |
| `energy_policy` | `{targets: [str], recent_changes: [str], sources: [url], content_sha1}` — `content_sha1`은 변경 감지에 쓴 원문 sha1(40자 hex) |
| `weekly_brief` | `{week_start, bullets: [str], evidence: [{doc_key, url}]}` (iso=`G20`) |
| `not_applicable` | `{reason}` — 중국·사우디(정당), 유로존(정부 지지율) |

## 6. 집계 규칙 (`webui/macro/aggregate.py`)
- `agg: last` → 기말값: D→M(월 마지막 관측), M→Q(분기 마지막 월), Q→Y(4분기, 진행 중 연도는 최신 분기 + `partial_period`).
- `agg: mean` → 기간 평균(여론조사, 환율 평균 토글용 `fx_usd_avg`는 API 파라미터로 계산).
- `yoy` → 같은 빈도의 12개월/4분기/1년 전 값 대비 %, 지수 원천에서 계산. **월별 YoY 평균 금지**.
- `index100(series, base_period)` → `v / v_base × 100`. 통화가치 지수 = `fx_base / fx × 100` (환율=현지통화/USD).
- 모든 함수는 순수 함수, 입력은 `list[Observation]` 또는 `list[(period, value)]`, 시간순 정렬 보장.

## 7. 소스 모듈 인터페이스 (`webui/macro/sources/<name>.py`)
```python
SOURCE_NAME = "bis"
CADENCE = "daily"   # daily | weekly | monthly | quarterly
def collect(countries: list[Country], indicators: list[Indicator], ctx: CollectContext) -> list[Observation]:
    """국가별 실패 격리: 한 국가 실패는 ctx.errors.append(...)로 기록하고 계속. 네트워크 timeout 30s, 재시도 2회."""
```
- `ctx.save_raw(source, name, data: bytes|str)` → S3 `macro/raw/<source>/<YYYY-MM-DD>/<name>.json.gz` (dry-run이면 로컬 `./.macro_raw/`).
- `ctx.since: date | None` (증분 수집 시작일), `ctx.dry_run: bool`, `ctx.errors: list[str]`, `ctx.log`.
- 키가 필요한 소스(`fred`: `FRED_API_KEY`, `ember`: `EMBER_API_KEY`)는 키가 없으면 빈 리스트 + 경고 1줄 (예외 금지).
- LLM 소스는 `webui/macro/llm/*.py`, 동일 인터페이스 + `ctx.llm.invoke_json(...)` 사용, `ctx.no_llm`이면 원문 수집만.

## 8. S3 레이아웃 (`DATA_BUCKET`)
```
macro/raw/<source>/<YYYY-MM-DD>/<name>.json.gz     원본 응답
macro/docs/<type>/<iso>/<date>_<id>.md            문서 전문(인용 포함)
macro/export/                                      (Phase 3, 비워둠)
```

## 9. Lambda API — `webui/backend/macro_api.py` (boto3만, `api_handler.py`가 `/api/macro/*`를 위임)
모든 응답: `{"ok": true, ...}` 또는 `{"error": "..."}`, 숫자는 float(Decimal 변환), 한국어 오류 메시지. 인증은 api_handler의 기존 게이트를 통과한 뒤 호출됨.

| 메서드·경로 | 응답 |
|---|---|
| `GET /api/macro/meta` | `{countries:[{iso,iso3,name_ko,ccy,groups,euro}], indicators:[{id,name_ko,unit,category,native_freq,store_freqs,agg,decimals}], ingest:{<source>:{status,finished_at,n_obs,next_due}}}` |
| `GET /api/macro/overview?group=G20&indicators=policy_rate,cpi_yoy,...` | `{asof:{M,Q,Y}, indicators:[{id,name_ko,unit,native_freq,store_freqs,decimals,category}], rows:[{iso, cells:{<indicator>:{value,period,freq,change,rank,flags,payload?}}}], median:{<indicator>:float}}` — 기본 지표 9개: `policy_rate,cpi_yoy,ppi_yoy,house_price_yoy,fx_value_index,m2_yoy,gdp_growth,mil_expenditure_share,party_support` |
| `GET /api/macro/countries/{iso}` | `{country:{...}, latest:{<indicator>:{...}}, docs:{<type>:{title_ko,summary_ko,date,source_url,source_name,ai_generated,review_status,confidence,quotes,payload}}, euro_ref?: "EU"}` — `docs`의 type은 `cb_stance, energy_policy, election, poll_of_polls, not_applicable?` |
| `GET /api/macro/series?indicator=policy_rate&countries=KR,US&freq=M&from=2016-01&to=2026-08&transform=level\|yoy\|index100&base=2020-01` (`from`·`to` 모두 지원, `index100`이면 `base`=시작 기간) | `{indicator:{...}, freq, series:{<iso>:[{period,value,flags?}]}, euro_shared:[iso...]}` — 유로 회원국 요청 시 EU 시계열을 그 iso 키로 복제하고 `euro_shared`에 표기 |
| `GET /api/macro/observations?indicator=&iso=&freq=&period=` | `{observation:{3장 전 필드 + revisions}, related_docs:[Doc 요약]}` |
| `GET /api/macro/docs?type=cb_stance&iso=KR&limit=12` | `{docs:[...]}` 최신순 |
| `GET /api/macro/politics/{iso}?from=2022-01` | `{election, poll_of_polls, polls:[Doc 객체 그대로 …최신 30건], series:{<정당명>:[{period(M),value}]}, not_applicable?}` — `series`는 월 평균 |
| `GET /api/macro/fx?base=2025-01-01&freq=M[&group=G20]` (`group` 생략 = G20 전체, `base`는 `YYYY-MM-DD`) | `{base, base_used:{<iso>: <기준으로 실제 사용된 기간>} (국가마다 기준 시점 관측 유무가 달라 dict), series:{<iso>:[{period,value(index)}]}, change:{<iso>:pct}, dxy:[{period,value}]}` — 유로존은 `EU` 키만(회원국 키 없어도 됨) |
| `POST /api/admin/macro/refresh {sources:[...], countries?:[...]}` | RunTask (카탈로그와 동일 방식, command `["python","webui/macro/collect.py","--sources",...,"--force"]`) → `{task_arn}`. 만료 전 `CONFIG / LOCK#collect`가 있으면 **409** `{"error":"이미 수집이 실행 중입니다. …"}`. RunTask 실패는 내부 예외 문자열 없이 고정 한국어 문구(500) |
| `POST /api/admin/macro/review {pk, sk, action: approve\|reject, note?}` | `{ok:true}` |

쿼리 제한: `countries` 최대 20, 기간 최대 30년(**일별 D는 10년**), `limit` 최대 100.
기간 상한은 **항상 강제**한다 — `/series`에서 `from`을 생략하면 `(to 또는 현재) − 30년`(D는 10년)이 기본 시작이 되어
응답 `from`에 그대로 담기고, 국가당 관측치가 4,000점을 넘으면 400. `/fx`의 `base`도 현재까지 30년(D 10년) 안이어야 하며 미래면 400.

### 9.1 프론트가 의존하는 응답 규칙 (백엔드는 이대로 구현한다)
프론트(`webui/frontend/macro.js`)가 이미 구현한 가정을 정식 계약으로 고정한다.
1. `GET /api/macro/series`는 `from`·`to`를 **둘 다** 받는다(각각 생략 가능, 범위 **최대 30년**).
   `transform=index100`이면 프론트는 `transform=index100&base=<from과 같은 기간 문자열>`로 호출한다 —
   `base`는 요청 시작 기간(예 `freq=M`이면 `2016-01`)이며, 그 기간 값이 없으면 그 이후 첫 관측을 기준으로 삼는다.
2. `GET /api/macro/fx`는 `group`을 **생략할 수 있다**(기본 = G20 전체 20개). `base`는 **`YYYY-MM-DD`** 날짜다.
   응답 `series`에는 유로존을 `EU` 하나로만 담는다(DE/FR/IT 키는 없어도 된다 — 프론트가 `euro_ref`로 처리).
3. `GET /api/macro/overview`의 `indicators`는 **객체 배열**
   `[{id, name_ko, unit, native_freq, store_freqs, decimals, category}]`이고(문자열 배열 금지),
   각 `rows[].cells[<indicator>]`는 `freq`·`period`를 **반드시** 포함한다(드로어가 이 둘로 OBS를 찾는다).
   값이 없는 (지표, 국가)는 셀 키를 생략한다 — 프론트는 '—'로 표시한다.
4. `GET /api/macro/countries/{iso}`의 `docs.<type>`은 **payload를 포함한 Doc 요약**이다:
   `{title_ko, summary_ko, date, source_url, source_name, ai_generated, review_status, confidence, quotes, payload}`
   + **`pk`, `sk`**(검토 API 호출용 — `observations.related_docs`·`/docs`·`politics.polls`의 문서에도 포함).
5. `GET /api/macro/politics/{iso}`의 `polls[]`는 **Doc 객체 그대로**다(문서 레벨 `date`·`source_url`·`ai_generated`·
   `review_status` + `payload`). `series`는 `{<정당명>: [{period(M), value}]}`이며 **월 평균**이다.
6. 금액 단위: `usd_bn`은 **10억 USD**, `usd`는 **USD 원단위**(`gdp_usd`·`gni_usd`·`gni_pc`·`mil_usd` = World Bank
   원값 그대로 — 스케일 변환은 프론트가 한다), `lcu_bn`은 자국통화 10억(국가 간 비교 금지).
7. `GET /api/macro/meta`의 `indicators` 항목에는 `store_freqs`·`category`가 **포함된다**.
8. 유로 회원국(DE/FR/IT)의 유로 공통 지표(`policy_rate`·`m2_*`·`fx_*`)는 회원국 OBS 항목이 **없다**.
   관측치 상세(`/api/macro/observations`, 드로어)는 `iso=EU`로 조회한다.
9. HTTP 200이어도 본문에 `error` 키가 있으면 프론트는 **오류로 취급**한다 → 성공 응답에 `error` 키를 넣지 않는다.

## 10. 환경변수
| 이름 | 사용처 | 값 |
|---|---|---|
| `MACRO_TABLE_NAME` | Lambda, 워커 | `!Ref MacroTable` |
| `DATA_BUCKET` | 기존 | |
| `HOME_REGION` | 워커(기존) | 서울 |
| `FRED_API_KEY`, `EMBER_API_KEY` | 워커 | CFN 파라미터(NoEcho) → 없으면 소스 스킵 |
| `MACRO_LLM_MODEL` | 워커 | 기본 `us.anthropic.claude-haiku-4-5-20251001-v1:0` (Bedrock us-east-1) |
| `MACRO_LLM_DAILY_TOKEN_BUDGET` | 워커 | 기본 `2000000` |
| `MACRO_DEV_NOAUTH` | devserver 전용 | 운영 코드에서 참조 금지 |

## 11. 프론트 계약 (`webui/frontend/macro.js`, `charts.js`)
- 진입: 탭 `#/macro`, 뷰 `#/macro`(개요) `#/macro/country/KR` `#/macro/indicator/policy_rate` `#/macro/fx` `#/macro/politics/KR`, 드로어 `?obs=<indicator>,<iso>,<freq>,<period>`
- app.js는 `parseHash`에 `/^#\/macro/`를 추가하고 `window.MacroView.route(hash, apiFetch, elem)`로 위임한다. 기존 뷰 동작 변경 금지.
- 색 규칙·차트 규격은 제안서 5.2절. 국가 고정색: KR `#3987e5` US `#d95926` JP `#199e70` CN `#c98500` EU `#d55181` GB `#008300` IN `#9085e9` BR `#e66767`, 나머지 `#8b949e`.
- 유로 회원국 카드에는 `euro_area_shared` 플래그 시 "유로존 공통" 배지. `ai_generated`면 "AI 판정/추출" 배지 + `review_status` 배지.
- 외부 링크(`source_url`, `payload.sources`, 브리프 `evidence.url`)는 `^(https?:)?//` 또는 `^https?:`(및 같은 출처 `/…`)만
  `<a href>`로 만들고, 그 외(`javascript:`·`data:` 등)는 텍스트로만 표시한다. CSV 내려받기 셀이 `= + - @ 탭 CR`로 시작하는
  **문자열**이면 `'`를 앞에 붙인다(수식 인젭션 방어 — 숫자 셀은 그대로).
- 히트맵 툴팁에는 `순위 rank/n`을 표시하고, 유로 복제 셀(`euro_area_shared`)은 "순위 —(유로존 공통)".
- 관리자(`deps.isAdmin()`)에게는 `review_status === 'pending'`인 문서 카드에 [승인]·[반려] 버튼을 보이고
  `POST /api/admin/macro/review {pk, sk, action}` 후 배지를 갱신한다. app.js는 mount 시 `{ apiFetch, elem, isAdmin }`을 넘긴다.

## 12. 수집 스케줄 (`collect.py` 내부 케이던스, EventBridge는 매일 21:00 UTC = 06:00 KST 1회 기동)
| source | CADENCE | 비고 |
|---|---|---|
| yahoo(fx, dxy) | daily | |
| bis(policy_rate D/M, fx_usd M, house_price Q) | daily(변경 감지) | |
| cb_statements(LLM) | daily 확인, 신규 문서만 LLM | |
| oecd(cpi, ppi), imf(m2), fred(US 폴백) | weekly | |
| ember, owid | monthly | |
| worldbank, wits, companies | monthly / quarterly | |
| wiki_polls(LLM) | weekly | |
| energy_policy, weekly_brief(LLM) | weekly | |
실행 순서: 정량 소스 → 파생(yoy, index, fuel_dep) → LLM 소스 → poll_of_polls 계산 → LATEST/SNAPSHOT 재생성 → INGEST 기록.
후처리 각 단계는 try/except로 격리되어 한 단계가 실패해도 다음 단계(특히 LATEST 재생성·INGEST 기록)는 실행되고,
실패 내용은 INGEST 요약의 `post_errors`에 남는다. 증분 실행(`since` 有)의 파생·집계는 이번 실행이 건드린 (지표, 국가)만,
조회 창은 `since − 시차`(yoy 12M/4Q/1Y, fx_value_index 12M, 집계는 target 버킷 시작)로 좁힌다.

**케이던스·`next_due` 규칙** (`INGEST#<source>` / `LATEST`, 판정은 `now >= next_due`)
- `next_due = (finished_at의 UTC 날짜 + cadence_days)의 00:00 UTC`. daily 1 · weekly 7 · monthly 30 · quarterly 90.
  시각이 아니라 **날짜 경계**로 판정해 매일 21:00 UTC 고정 기동에서 daily 소스가 격일로 밀리지 않는다
  (예: day1 21:03 종료 → next_due day2 00:00 → day2 21:00 기동에서 실행).
- `status`가 `failed`/`partial`이면 `cadence_days`는 그대로 두고 `next_due`만 **다음 날 00:00 UTC**로 앞당겨
  다음 기동에서 재시도한다. LATEST에 `retry_reason`을 남기고, 성공하면 정상 케이던스로 돌아간다.
- 동시 실행 방지: 기동 시 `CONFIG / LOCK#collect`(4장) 획득에 실패하면 어떤 소스도 실행하지 않고 exit 0.

**저빈도 집계 가드** — 저장된 target 항목과 집계 결과의 `source`가 다르면 덮지 않는다는 규칙은 **같은 버킷(period)** 끼리만
비교한다(가장 최신 항목과 비교하면 poll_of_polls가 만든 이번 달 `derived` M 때문에 지난달 W→M 집계가 영구 스킵됨).
여론조사(W) 원천은 진행 중인 달만 poll_of_polls의 몫으로 남기고 집계에서 제외한다.

**폴백 체인** (2026-09-20 소스 실측 기준. 자세한 커버리지는 `registry.yaml`의 `coverage_note`)
- `fx_usd` **월별**: BIS `WS_XRU/1.0/M.{bis}.{ccy}.E`(기말) 1차 → 실패 시 yahoo 일별을 **D→M 기말 집계**해 채우고
  `flags: [fallback_source]`. OBS 키(`OBS#fx_usd#<iso>` / `M#YYYY-MM`)가 같아 소스가 달라도 같은 자리를 덮어쓴다.
- `fx_usd` **US**: 기준통화라 어떤 소스도 내지 않으므로 수집기가 다른 국가 월별 관측의 기간 범위로
  `value 1.0`(`source: derived`, `series_id: USD_BASE`, `unit: lcu_per_usd`, `flags: [derived]`, M만)을 합성한다
  → 파생 단계에서 `fx_value_index` US = 100이 생기고 순위에도 포함된다.
- `m2_level`/`m2_yoy`: IMF MFS(월, 자국통화 `lcu_bn`, 11/20) → FRED `M2SL`(US만, `usd_bn`) →
  World Bank `FM.LBL.BMNY.ZG`(연간, `m2_yoy`만). EU·CN·GB·IN·SA는 월별 소스 미구현.
- `cpi_index`/`cpi_yoy`: OECD(`DF_G20_PRICES` → `DF_PRICES_C2018_ALL` → `DF_PRICES_ALL`, **국가별로 하나 고정**) →
  World Bank `FP.CPI.TOTL.ZG` 연간. RU는 연간만(월별 원천이 2022-03에서 정지).
- `ppi_index`/`ppi_yoy`: FRED(US, 최신) → OECD KEI(**2023-02까지만**). KR·JP·CN·IN은 어느 쪽에도 없다.
- `elec_mix`: Ember API(키 필요 · ISO3 19개국은 `entity_code`, 유로존은 집계 엔티티 `entity=EU`로 **요청 분리**
  — 한 요청에 섞으면 0행) → **API 응답에 없는 국가만** Ember 공개 CSV로 보충(`fallback_source`)
  → 키가 없거나 API 요청 실패면 공개 CSV 전체(무키, `fallback_source`, 유로존 Area `EU`) → OWID 연간.
- `top_companies`: 카탈로그(KR/JP/US/CN) → yahoo(14개국). RU·EU는 없음.
- 어떤 소스도 값을 주지 못하는 (지표, 국가) — 예: `policy_rate` **AR** — 은 `LATEST#<indicator>`에 항목을 만들지
  않는다. 프론트는 그 셀을 '—'로 표시한다.
