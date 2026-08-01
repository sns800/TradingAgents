# =============================================================================
# [모듈 개요 — 초보자용]
# 이 파일은 TradingAgents의 "기억(memory)" 역할을 하는 추가 전용(append-only)
# 마크다운 결정 로그를 구현합니다. 매 실행이 끝나면 최종 매매 결정을 로그 파일에
# 기록하고(Phase A), 나중에 실제 수익률이 확정되면 결과와 반성(reflection)을
# 덧붙입니다(Phase B). 다음 실행 시작 시 같은 종목의 과거 결정과 다른 종목의
# 교훈을 읽어 에이전트 프롬프트에 주입함으로써, 시스템이 과거 실수에서
# 배우도록 합니다. (임베딩/벡터 DB 없이 텍스트 파싱만으로 동작하는 단순한 구조)
# =============================================================================

"""TradingAgents용 추가 전용(append-only) 마크다운 결정 로그."""

import re
from pathlib import Path

from tradingagents.agents.utils.rating import parse_rating


class TradingMemoryLog:
    """매매 결정과 반성(reflection)을 기록하는 추가 전용 마크다운 로그."""

    # HTML 주석: LLM의 서술 출력에 나타날 수 없으므로 안전한 확정 구분자(delimiter)
    _SEPARATOR = "\n\n<!-- ENTRY_END -->\n\n"
    # 미리 컴파일한 정규식 패턴 — load_entries() 호출 때마다 재컴파일되는 것을 방지
    _DECISION_RE = re.compile(r"DECISION:\n(.*?)(?=\nREFLECTION:|\Z)", re.DOTALL)
    _REFLECTION_RE = re.compile(r"REFLECTION:\n(.*?)$", re.DOTALL)
    # 자산군(asset class) 태그 줄. DECISION 본문 앞 헤더 영역에서만 찾는다.
    _ASSET_RE = re.compile(r"^ASSET:\s*(\S+)\s*$", re.MULTILINE)
    # past_context에 주입할 때 DECISION 원문을 자르는 길이(문자 수, 결정론적).
    # 교훈의 핵심은 REFLECTION이므로 결정문 전문 주입은 토큰만 낭비하고
    # 직전 등급에 앵커링(anchoring)시키는 부작용이 있다.
    _DECISION_SNIPPET_CHARS = 300

    def __init__(self, config: dict = None):
        cfg = config or {}
        self._log_path = None
        path = cfg.get("memory_log_path")
        if path:
            self._log_path = Path(path).expanduser()
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
        # 결과 확정(resolved) 항목 수의 선택적 상한. None이면 로테이션(rotation) 비활성화.
        self._max_entries = cfg.get("memory_log_max_entries")

    # --- 쓰기 경로 (Phase A) ---

    def store_decision(
        self,
        ticker: str,
        trade_date: str,
        final_trade_decision: str,
        asset_type: str = "stock",
    ) -> None:
        """propagate() 종료 시점에 대기(pending) 항목을 추가한다. LLM 호출 없음.

        ``asset_type``(stock/crypto)은 항목 헤더의 ``ASSET:`` 줄로 저장되어,
        나중에 cross-ticker 교훈을 같은 자산군으로만 선별하는 데 쓰입니다.
        태그가 없는 구형 항목은 파싱 시 stock으로 간주됩니다(하위 호환).
        """
        if not self._log_path:
            return
        # 멱등성(idempotency) 가드: 전체 파싱 대신 빠른 원문 텍스트 스캔
        if self._log_path.exists():
            raw = self._log_path.read_text(encoding="utf-8")
            for line in raw.splitlines():
                if line.startswith(f"[{trade_date} | {ticker} |") and line.endswith("| pending]"):
                    return
        rating = parse_rating(final_trade_decision, context="memory log entry")
        tag = f"[{trade_date} | {ticker} | {rating} | pending]"
        entry = (
            f"{tag}\n\nASSET: {asset_type}\n\n"
            f"DECISION:\n{final_trade_decision}{self._SEPARATOR}"
        )
        with open(self._log_path, "a", encoding="utf-8") as f:
            f.write(entry)

    # --- 읽기 경로 (Phase A) ---

    def load_entries(self) -> list[dict]:
        """로그에서 모든 항목을 파싱한다. dict의 리스트를 반환한다."""
        if not self._log_path or not self._log_path.exists():
            return []
        text = self._log_path.read_text(encoding="utf-8")
        raw_entries = [e.strip() for e in text.split(self._SEPARATOR) if e.strip()]
        entries = []
        for raw in raw_entries:
            parsed = self._parse_entry(raw)
            if parsed:
                entries.append(parsed)
        return entries

    def get_pending_entries(self) -> list[dict]:
        """결과가 pending(대기) 상태인 항목을 반환한다(Phase B에서 사용)."""
        return [e for e in self.load_entries() if e.get("pending")]

    def get_past_context(
        self, ticker: str, n_same: int = 5, n_cross: int = 3,
        asset_type: str = "stock",
    ) -> str:
        """에이전트 프롬프트 주입용으로 포맷된 과거 컨텍스트 문자열을 반환한다.

        결과가 확정된(pending 아님) 항목만 대상으로 최근순으로 모읍니다.
        같은 종목(same)은 REFLECTION 전문 + DECISION 앞부분 요약으로 축약해
        주입하고, 다른 종목(cross)은 반성만 짧게 주입하되 ``asset_type``이
        같은 자산군(stock/crypto)의 항목만 선별합니다 — crypto 교훈이 주식
        결정에 주입되는 것을 막습니다. ASSET 태그가 없는 구형 항목은
        stock으로 간주합니다(하위 호환).
        """
        entries = [e for e in self.load_entries() if not e.get("pending")]
        if not entries:
            return ""

        same, cross = [], []
        for e in reversed(entries):
            if len(same) >= n_same and len(cross) >= n_cross:
                break
            if e["ticker"] == ticker and len(same) < n_same:
                same.append(e)
            elif (
                e["ticker"] != ticker
                and e.get("asset_type", "stock") == asset_type
                and len(cross) < n_cross
            ):
                cross.append(e)

        if not same and not cross:
            return ""

        parts = []
        if same:
            parts.append(f"Past analyses of {ticker} (most recent first):")
            parts.extend(self._format_condensed(e) for e in same)
        if cross:
            parts.append("Recent cross-ticker lessons:")
            parts.extend(self._format_reflection_only(e) for e in cross)
        return "\n\n".join(parts)

    # --- 갱신 경로 (Phase B) ---

    def update_with_outcome(
        self,
        ticker: str,
        trade_date: str,
        raw_return: float,
        alpha_return: float,
        holding_days: int,
        reflection: str,
    ) -> None:
        """pending 태그를 교체하고 원자적 쓰기(atomic write)로 REFLECTION 섹션을 덧붙인다.

        (trade_date, ticker)와 일치하는 첫 번째 pending 항목을 찾아 태그를
        수익률 수치로 갱신하고 REFLECTION 섹션을 덧붙인다. 임시 파일 +
        os.replace() 방식을 사용해 쓰기 도중 크래시가 나도 로그가 절대
        손상되지 않는다.
        """
        if not self._log_path or not self._log_path.exists():
            return

        text = self._log_path.read_text(encoding="utf-8")
        blocks = text.split(self._SEPARATOR)

        pending_prefix = f"[{trade_date} | {ticker} |"
        raw_pct = f"{raw_return:+.1%}"
        alpha_pct = f"{alpha_return:+.1%}"

        updated = False
        new_blocks = []
        for block in blocks:
            stripped = block.strip()
            if not stripped:
                new_blocks.append(block)
                continue

            lines = stripped.splitlines()
            tag_line = lines[0].strip()

            if (
                not updated
                and tag_line.startswith(pending_prefix)
                and tag_line.endswith("| pending]")
            ):
                # 기존 pending 태그에서 등급(rating)을 파싱한다
                fields = [f.strip() for f in tag_line[1:-1].split("|")]
                rating = fields[2]
                new_tag = (
                    f"[{trade_date} | {ticker} | {rating}"
                    f" | {raw_pct} | {alpha_pct} | {holding_days}d]"
                )
                rest = "\n".join(lines[1:])
                new_blocks.append(
                    f"{new_tag}\n\n{rest.lstrip()}\n\nREFLECTION:\n{reflection}"
                )
                updated = True
            else:
                new_blocks.append(block)

        if not updated:
            return

        new_blocks = self._apply_rotation(new_blocks)
        new_text = self._SEPARATOR.join(new_blocks)
        tmp_path = self._log_path.with_suffix(".tmp")
        tmp_path.write_text(new_text, encoding="utf-8")
        tmp_path.replace(self._log_path)

    def batch_update_with_outcomes(self, updates: list[dict]) -> None:
        """여러 결과 갱신을 한 번의 읽기 + 원자적 쓰기로 일괄 적용한다.

        updates의 각 원소는 다음 키를 가져야 한다: ticker, trade_date,
        raw_return, alpha_return, holding_days, reflection.
        """
        if not self._log_path or not self._log_path.exists() or not updates:
            return

        text = self._log_path.read_text(encoding="utf-8")
        blocks = text.split(self._SEPARATOR)

        # (trade_date, ticker)를 키로 하는 조회 테이블을 만들어 O(1) 매칭
        update_map = {(u["trade_date"], u["ticker"]): u for u in updates}

        new_blocks = []
        for block in blocks:
            stripped = block.strip()
            if not stripped:
                new_blocks.append(block)
                continue

            lines = stripped.splitlines()
            tag_line = lines[0].strip()

            matched = False
            for (trade_date, ticker), upd in list(update_map.items()):
                pending_prefix = f"[{trade_date} | {ticker} |"
                if tag_line.startswith(pending_prefix) and tag_line.endswith("| pending]"):
                    fields = [f.strip() for f in tag_line[1:-1].split("|")]
                    rating = fields[2]
                    raw_pct = f"{upd['raw_return']:+.1%}"
                    alpha_pct = f"{upd['alpha_return']:+.1%}"
                    new_tag = (
                        f"[{trade_date} | {ticker} | {rating}"
                        f" | {raw_pct} | {alpha_pct} | {upd['holding_days']}d]"
                    )
                    rest = "\n".join(lines[1:])
                    new_blocks.append(
                        f"{new_tag}\n\n{rest.lstrip()}\n\nREFLECTION:\n{upd['reflection']}"
                    )
                    del update_map[(trade_date, ticker)]
                    matched = True
                    break

            if not matched:
                new_blocks.append(block)

        new_blocks = self._apply_rotation(new_blocks)
        new_text = self._SEPARATOR.join(new_blocks)
        tmp_path = self._log_path.with_suffix(".tmp")
        tmp_path.write_text(new_text, encoding="utf-8")
        tmp_path.replace(self._log_path)

    # --- 헬퍼(Helpers) ---

    def _apply_rotation(self, blocks: list[str]) -> list[str]:
        """결과 확정 블록 수가 max_entries를 초과하면 가장 오래된 것부터 버린다.

        pending 블록은 항상 유지한다(아직 처리되지 않은 작업을 나타내므로).
        로테이션이 비활성화됐거나 상한 이하이면 ``blocks``를 그대로 반환한다.
        """
        if not self._max_entries or self._max_entries <= 0:
            return blocks

        # 태그 줄 마커를 파싱해 각 블록에 (블록, 결과확정여부)를 표시한다.
        decisions = []
        for block in blocks:
            stripped = block.strip()
            if not stripped:
                decisions.append((block, False))
                continue
            tag_line = stripped.splitlines()[0].strip()
            is_resolved = (
                tag_line.startswith("[")
                and tag_line.endswith("]")
                and not tag_line.endswith("| pending]")
            )
            decisions.append((block, is_resolved))

        resolved_count = sum(1 for _, r in decisions if r)
        if resolved_count <= self._max_entries:
            return blocks

        to_drop = resolved_count - self._max_entries
        kept: list[str] = []
        for block, is_resolved in decisions:
            if is_resolved and to_drop > 0:
                to_drop -= 1
                continue
            kept.append(block)
        return kept

    def _parse_entry(self, raw: str) -> dict | None:
        # 항목 하나의 원문 텍스트를 dict로 파싱한다.
        # 첫 줄은 "[날짜 | 티커 | 등급 | ...]" 형태의 태그 줄이어야 한다.
        lines = raw.strip().splitlines()
        if not lines:
            return None
        tag_line = lines[0].strip()
        if not (tag_line.startswith("[") and tag_line.endswith("]")):
            return None
        fields = [f.strip() for f in tag_line[1:-1].split("|")]
        if len(fields) < 4:
            return None
        entry = {
            "date": fields[0],
            "ticker": fields[1],
            "rating": fields[2],
            "pending": fields[3] == "pending",
            "raw": fields[3] if fields[3] != "pending" else None,
            "alpha": fields[4] if len(fields) > 4 else None,
            "holding": fields[5] if len(fields) > 5 else None,
        }
        body = "\n".join(lines[1:]).strip()
        # ASSET 태그는 DECISION 본문 앞 헤더 영역에서만 찾는다 — 결정문 안에
        # 우연히 "ASSET:"으로 시작하는 줄이 있어도 오인하지 않도록.
        header = body.split("DECISION:", 1)[0]
        asset_match = self._ASSET_RE.search(header)
        # 태그가 없는 구형 항목은 stock으로 간주(하위 호환)
        entry["asset_type"] = asset_match.group(1).lower() if asset_match else "stock"
        decision_match = self._DECISION_RE.search(body)
        reflection_match = self._REFLECTION_RE.search(body)
        entry["decision"] = decision_match.group(1).strip() if decision_match else ""
        entry["reflection"] = reflection_match.group(1).strip() if reflection_match else ""
        return entry

    def _truncate_decision(self, text: str) -> str:
        # DECISION 원문을 결정론적으로 앞부분만 자른다(항상 같은 입력 → 같은 출력).
        if len(text) <= self._DECISION_SNIPPET_CHARS:
            return text
        return text[: self._DECISION_SNIPPET_CHARS].rstrip() + "..."

    def _format_condensed(self, e: dict) -> str:
        # 같은 종목의 과거 항목용 축약 포맷: 태그 + REFLECTION 전문 +
        # DECISION 앞부분 요약. 교훈(REFLECTION)이 핵심이므로 앞세우고,
        # 결정문 전문 대신 앞 300자만 넣어 신호 대 잡음비를 지킨다.
        raw = e["raw"] or "n/a"
        alpha = e["alpha"] or "n/a"
        holding = e["holding"] or "n/a"
        tag = f"[{e['date']} | {e['ticker']} | {e['rating']} | {raw} | {alpha} | {holding}]"
        parts = [tag]
        if e["reflection"]:
            parts.append(f"REFLECTION:\n{e['reflection']}")
        parts.append(f"DECISION:\n{self._truncate_decision(e['decision'])}")
        return "\n\n".join(parts)

    def _format_reflection_only(self, e: dict) -> str:
        # 다른 종목(cross-ticker) 항목용: 태그 + 반성만 짧게 포맷
        # (반성이 없으면 결정 내용 앞 300자로 대체)
        tag = f"[{e['date']} | {e['ticker']} | {e['rating']} | {e['raw'] or 'n/a'}]"
        if e["reflection"]:
            return f"{tag}\n{e['reflection']}"
        return f"{tag}\n{self._truncate_decision(e['decision'])}"
