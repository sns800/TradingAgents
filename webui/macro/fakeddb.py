# ============================================================
# [모듈 개요] 인메모리 DynamoDB 테이블 / S3 페이크 — 테스트·devserver·dry-run 공용
#
# moto를 의존성에 추가하지 않고도 `webui/macro/store.py`(MacroStore)와 수집기·API를
# 오프라인에서 돌릴 수 있게, boto3 리소스의 **호출 규약만 정확히** 흉내 내는 최소 구현.
#
# 설계 원칙
#  1) 시그니처·반환 형태는 실제 boto3와 동일 (`get_item` → {"Item": ...} 또는 {},
#     `query` → {"Items", "Count", "LastEvaluatedKey"?}). 호출부를 페이크용으로
#     분기하지 않는다.
#  2) 실패도 흉내 낸다: 조건부 쓰기 실패는 botocore ClientError
#     (Code=ConditionalCheckFailedException), **float 저장은 TypeError**.
#     (DynamoDB put_item은 float를 받지 않는다 — Decimal 누락 버그를 테스트에서 잡는다.)
#  3) 지원하지 않는 표현식은 조용히 무시하지 말고 NotImplementedError로 알린다.
#  4) query는 pk별 sk 인덱스(dict[pk] → 정렬된 sk 리스트, 쓰기 시 bisect 유지)로 후보를
#     좁힌다. 전 항목 정렬은 하지 않는다 (55k 항목·4.5k 쿼리 221초 → 4초 미만).
#
# 지원 범위 (UpdateExpression)
#   SET  a = :v / #a = :v / a.b = :v, if_not_exists(a, :v), list_append(a, :v),
#        operand +/- operand
#   ADD  n :inc            (숫자만)
#   REMOVE a, #b, a.b
#   DELETE → NotImplementedError
# 지원 범위 (Condition/KeyCondition/Filter)
#   boto3 Key()/Attr() 조건 객체: = <> < <= > >= BETWEEN begins_with contains IN
#                                 attribute_exists attribute_not_exists attribute_type
#                                 AND OR NOT
#   문자열: "attribute_not_exists(pk)", "attribute_exists(pk)", "begins_with(a, :v)",
#           "#a = :v" 같은 단순 비교 + 최상위 AND/OR (괄호 없음)
#
# 사용 예:
#   table = FakeTable()                      # pk/sk 단일 테이블
#   store = MacroStore(table, s3=FakeS3(), bucket="b")
#   FakeTable(page_size=2)                   # 페이지네이션 처리 테스트용
# ============================================================
from __future__ import annotations

import bisect
import copy
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from boto3.dynamodb.conditions import AttributeBase, ConditionBase
from botocore.exceptions import ClientError

FLOAT_MSG = "Float types are not supported. Use Decimal types instead."


# ---------------------------------------------------------------- 공통 유틸


def _client_error(code: str, message: str, op: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, op)


def _reject_floats(obj: Any, path: str = "") -> None:
    """boto3 직렬화기와 같은 지점에서 float를 거부한다 (Decimal 누락 탐지)."""
    if isinstance(obj, float):
        raise TypeError(f"{FLOAT_MSG} (at {path or 'root'})")
    if isinstance(obj, dict):
        for k, v in obj.items():
            _reject_floats(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, (list, tuple, set)):
        for i, v in enumerate(obj):
            _reject_floats(v, f"{path}[{i}]")


def _num(v: Any) -> Decimal:
    if isinstance(v, Decimal):
        return v
    if isinstance(v, bool) or not isinstance(v, int):
        raise TypeError(f"expected a number, got {type(v).__name__}")
    return Decimal(v)


def _comparable(a: Any, b: Any) -> bool:
    """DynamoDB는 타입이 다르면 비교 조건을 거짓으로 취급한다."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool)
    num = (int, float, Decimal)
    if isinstance(a, num) and isinstance(b, num):
        return True
    return type(a) is type(b) and isinstance(a, (str, bytes))


# ------------------------------------------------------- 경로(path) 접근자


_MISSING = object()


def _split_path(path: str, names: dict[str, str] | None) -> list[str]:
    parts = []
    for raw in path.split("."):
        tok = raw.strip()
        if not tok:
            raise NotImplementedError(f"unsupported document path: {path!r}")
        if tok.startswith("#"):
            if not names or tok not in names:
                raise _client_error(
                    "ValidationException",
                    f"ExpressionAttributeNames에 {tok} 없음",
                    "UpdateItem",
                )
            tok = names[tok]
        if "[" in tok:  # 리스트 인덱스 경로는 미지원
            raise NotImplementedError(f"list index paths are not supported: {path!r}")
        parts.append(tok)
    return parts


def _get_path(item: dict | None, parts: list[str]) -> Any:
    cur: Any = item if item is not None else {}
    for p in parts:
        if not isinstance(cur, dict) or p not in cur:
            return _MISSING
        cur = cur[p]
    return cur


def _set_path(item: dict, parts: list[str], value: Any) -> None:
    cur = item
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            if p in cur:
                raise _client_error(
                    "ValidationException", f"{p}는 map이 아니라 경로를 만들 수 없음", "UpdateItem"
                )
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _del_path(item: dict, parts: list[str]) -> None:
    cur: Any = item
    for p in parts[:-1]:
        if not isinstance(cur, dict) or p not in cur:
            return
        cur = cur[p]
    if isinstance(cur, dict):
        cur.pop(parts[-1], None)


# ------------------------------------------------------------- 조건식 평가


def _eval_condition(
    cond: Any,
    item: dict | None,
    names: dict[str, str] | None = None,
    values: dict[str, Any] | None = None,
) -> bool:
    if cond is None:
        return True
    if isinstance(cond, ConditionBase):
        return _eval_cond_obj(cond, item)
    if isinstance(cond, str):
        return _eval_cond_str(cond, item, names, values)
    raise NotImplementedError(f"unsupported condition type {type(cond).__name__}")


def _operand(v: Any, item: dict | None) -> Any:
    if isinstance(v, AttributeBase):
        return _get_path(item, _split_path(v.name, None))
    return v


def _eval_cond_obj(cond: ConditionBase, item: dict | None) -> bool:
    expr = cond.get_expression()
    op = expr["operator"]
    vals = expr["values"]
    if op == "AND":
        return all(_eval_cond_obj(v, item) for v in vals)
    if op == "OR":
        return any(_eval_cond_obj(v, item) for v in vals)
    if op == "NOT":
        return not _eval_cond_obj(vals[0], item)

    left = _operand(vals[0], item)
    if op == "attribute_exists":
        return left is not _MISSING
    if op == "attribute_not_exists":
        return left is _MISSING
    if op == "attribute_type":
        return left is not _MISSING and _dynamo_type(left) == vals[1]
    if left is _MISSING:
        return False
    if op == "BETWEEN":
        lo, hi = _operand(vals[1], item), _operand(vals[2], item)
        if not (_comparable(left, lo) and _comparable(left, hi)):
            return False
        return lo <= left <= hi
    if op == "IN":
        candidates = vals[1] if isinstance(vals[1], (list, tuple)) else vals[1:]
        return any(_comparable(left, c) and left == c for c in candidates)

    right = _operand(vals[1], item) if len(vals) > 1 else None
    if right is _MISSING:
        return op == "<>"
    if op == "begins_with":
        return isinstance(left, str) and isinstance(right, str) and left.startswith(right)
    if op == "contains":
        if isinstance(left, (list, set, tuple)):
            return right in left
        return isinstance(left, str) and isinstance(right, str) and right in left
    if op == "=":
        return _comparable(left, right) and left == right
    if op == "<>":
        return not (_comparable(left, right) and left == right)
    if op in ("<", "<=", ">", ">="):
        if not _comparable(left, right):
            return False
        return {
            "<": left < right,
            "<=": left <= right,
            ">": left > right,
            ">=": left >= right,
        }[op]
    raise NotImplementedError(f"unsupported condition operator {op!r}")


def _dynamo_type(v: Any) -> str:
    if isinstance(v, bool):
        return "BOOL"
    if isinstance(v, (int, Decimal, float)):
        return "N"
    if isinstance(v, str):
        return "S"
    if isinstance(v, bytes):
        return "B"
    if isinstance(v, list):
        return "L"
    if isinstance(v, dict):
        return "M"
    if v is None:
        return "NULL"
    return "?"


_STR_FUNC_RE = re.compile(
    r"^(attribute_not_exists|attribute_exists|begins_with|contains)\s*\((.*)\)$", re.I
)
_STR_CMP_RE = re.compile(r"^(.+?)\s*(<>|<=|>=|=|<|>)\s*(.+)$")


def _eval_cond_str(
    expr: str, item: dict | None, names: dict[str, str] | None, values: dict[str, Any] | None
) -> bool:
    expr = expr.strip()
    for joiner, reducer in ((" OR ", any), (" AND ", all)):
        parts = _split_top_level(expr, joiner)
        if len(parts) > 1:
            return reducer(_eval_cond_str(p, item, names, values) for p in parts)
    if expr.startswith("(") and expr.endswith(")"):
        return _eval_cond_str(expr[1:-1], item, names, values)

    m = _STR_FUNC_RE.match(expr)
    if m:
        fn = m.group(1).lower()
        args = [a.strip() for a in _split_top_level(m.group(2), ",")]
        left = _str_operand(args[0], item, names, values)
        if fn == "attribute_exists":
            return left is not _MISSING
        if fn == "attribute_not_exists":
            return left is _MISSING
        right = _str_operand(args[1], item, names, values)
        if left is _MISSING or right is _MISSING:
            return False
        if fn == "begins_with":
            return isinstance(left, str) and left.startswith(right)
        return right in left if isinstance(left, (str, list, set, tuple)) else False

    m = _STR_CMP_RE.match(expr)
    if m:
        left = _str_operand(m.group(1).strip(), item, names, values)
        right = _str_operand(m.group(3).strip(), item, names, values)
        op = m.group(2)
        if left is _MISSING or right is _MISSING:
            return op == "<>" and not (left is _MISSING and right is _MISSING)
        if not _comparable(left, right):
            return False
        return {
            "=": left == right,
            "<>": left != right,
            "<": left < right,
            "<=": left <= right,
            ">": left > right,
            ">=": left >= right,
        }[op]
    raise NotImplementedError(f"unsupported ConditionExpression string: {expr!r}")


def _str_operand(
    tok: str, item: dict | None, names: dict[str, str] | None, values: dict[str, Any] | None
) -> Any:
    if tok.startswith(":"):
        if not values or tok not in values:
            raise _client_error(
                "ValidationException", f"ExpressionAttributeValues에 {tok} 없음", "UpdateItem"
            )
        return values[tok]
    return _get_path(item, _split_path(tok, names))


def _split_top_level(s: str, sep: str) -> list[str]:
    """괄호 깊이를 고려해 최상위 구분자만으로 나눈다 (list_append(a, :v) 보호)."""
    out, depth, buf, i = [], 0, [], 0
    n, m = len(s), len(sep)
    while i < n:
        ch = s[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if depth == 0 and s[i : i + m] == sep:
            out.append("".join(buf))
            buf = []
            i += m
            continue
        buf.append(ch)
        i += 1
    out.append("".join(buf))
    return [p for p in (x.strip() for x in out) if p != ""] or [""]


# ------------------------------------------------------ UpdateExpression 파서

_ACTION_RE = re.compile(r"\b(SET|ADD|REMOVE|DELETE)\b", re.I)
_ARITH_RE = re.compile(r"\s+([+-])\s+")
_FUNC_CALL_RE = re.compile(r"^(if_not_exists|list_append)\s*\((.*)\)$", re.I)


def _parse_update_expression(expr: str) -> list[tuple[str, str]]:
    matches = list(_ACTION_RE.finditer(expr))
    if not matches or expr[: matches[0].start()].strip():
        raise NotImplementedError(f"unsupported UpdateExpression: {expr!r}")
    sections = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(expr)
        sections.append((m.group(1).upper(), expr[m.end() : end].strip()))
    return sections


class _Updater:
    """update_item의 제한 파서. 미지원 문법은 NotImplementedError."""

    def __init__(self, item: dict, names: dict[str, str] | None, values: dict[str, Any] | None):
        self.item = item
        self.names = names or {}
        self.values = values or {}
        self.touched: dict[str, Any] = {}
        self.removed: list[str] = []

    def apply(self, expr: str) -> None:
        for action, body in _parse_update_expression(expr):
            if action == "SET":
                for clause in _split_top_level(body, ","):
                    self._set(clause)
            elif action == "ADD":
                for clause in _split_top_level(body, ","):
                    self._add(clause)
            elif action == "REMOVE":
                for clause in _split_top_level(body, ","):
                    self._remove(clause)
            else:
                raise NotImplementedError(f"unsupported update action {action}")

    # -- 액션별 처리
    def _set(self, clause: str) -> None:
        lhs, _, rhs = clause.partition("=")
        if not rhs:
            raise NotImplementedError(f"unsupported SET clause: {clause!r}")
        parts = _split_path(lhs.strip(), self.names)
        value = self._value(rhs.strip())
        if value is _MISSING:
            raise _client_error(
                "ValidationException", f"SET 대상 값을 계산할 수 없음: {clause!r}", "UpdateItem"
            )
        _set_path(self.item, parts, value)
        self.touched[parts[0]] = self.item.get(parts[0])

    def _add(self, clause: str) -> None:
        toks = clause.split()
        if len(toks) != 2:
            raise NotImplementedError(f"unsupported ADD clause: {clause!r}")
        parts = _split_path(toks[0], self.names)
        delta = self._value(toks[1])
        cur = _get_path(self.item, parts)
        if isinstance(delta, (set, frozenset)) or isinstance(cur, (set, frozenset)):
            raise NotImplementedError("ADD for set types is not supported")
        base = Decimal(0) if cur is _MISSING else _num(cur)
        _set_path(self.item, parts, base + _num(delta))
        self.touched[parts[0]] = self.item.get(parts[0])

    def _remove(self, clause: str) -> None:
        parts = _split_path(clause.strip(), self.names)
        _del_path(self.item, parts)
        self.removed.append(parts[0])

    # -- 값 계산
    def _value(self, tok: str) -> Any:
        tok = tok.strip()
        arith = _split_top_level(_ARITH_RE.sub(r" \1 ", tok), " ")
        if len(arith) == 3 and arith[1] in ("+", "-"):
            left, right = _num(self._value(arith[0])), _num(self._value(arith[2]))
            return left + right if arith[1] == "+" else left - right
        m = _FUNC_CALL_RE.match(tok)
        if m:
            fn = m.group(1).lower()
            args = _split_top_level(m.group(2), ",")
            if len(args) != 2:
                raise NotImplementedError(f"unsupported function call: {tok!r}")
            if fn == "if_not_exists":
                cur = _get_path(self.item, _split_path(args[0], self.names))
                return self._value(args[1]) if cur is _MISSING else cur
            a, b = self._value(args[0]), self._value(args[1])
            if not isinstance(a, list) or not isinstance(b, list):
                raise _client_error(
                    "ValidationException", "list_append 인자는 리스트여야 함", "UpdateItem"
                )
            return a + b
        if tok.startswith(":"):
            if tok not in self.values:
                raise _client_error(
                    "ValidationException", f"ExpressionAttributeValues에 {tok} 없음", "UpdateItem"
                )
            return self.values[tok]
        if tok.startswith("#") or re.fullmatch(r"[\w.]+", tok):
            return _get_path(self.item, _split_path(tok, self.names))
        raise NotImplementedError(f"unsupported value expression: {tok!r}")


# -------------------------------------------------------------- FakeTable


class FakeTable:
    """boto3 DynamoDB `Table` 리소스의 부분집합 (pk/sk 단일 테이블).

    `items`는 읽기 전용으로 보는 것만 안전하다 — 쓰기는 put/update/delete/batch_writer로.
    (`_sk_index`가 pk별 정렬된 sk를 들고 있어 직접 변경하면 query 결과가 어긋난다.)
    """

    def __init__(
        self,
        items: list[dict] | None = None,
        *,
        name: str = "tradingagents-webui-macro",
        hash_key: str = "pk",
        range_key: str | None = "sk",
        page_size: int = 1000,
    ):
        self.name = name
        self.table_name = name
        self.hash_key = hash_key
        self.range_key = range_key
        self.page_size = page_size
        self.items: dict[tuple, dict] = {}
        self.calls: dict[str, int] = {}
        # pk → 정렬된 sk 목록. 쓰기 시점에 bisect로 유지해 query가 전 항목을 다시
        # 정렬하지 않게 한다 (실측: 55k 항목·4.5k 쿼리 221초 → 1초 미만).
        self._sk_index: dict[Any, list[Any]] = {}
        for it in items or []:
            self._store(self._key_tuple(it), copy.deepcopy(it))

    # -- 내부 헬퍼
    def _count(self, op: str) -> None:
        self.calls[op] = self.calls.get(op, 0) + 1

    def _key_tuple(self, d: dict) -> tuple:
        try:
            if self.range_key is None:
                return (d[self.hash_key],)
            return (d[self.hash_key], d[self.range_key])
        except KeyError as e:
            raise _client_error(
                "ValidationException", f"키 속성 {e.args[0]!r} 누락", "PutItem"
            ) from None

    def _key_of(self, item: dict) -> dict:
        key = {self.hash_key: item[self.hash_key]}
        if self.range_key is not None and self.range_key in item:
            key[self.range_key] = item[self.range_key]
        return key

    def _store(self, key: tuple, item: dict) -> None:
        """항목을 넣고 pk별 sk 인덱스를 갱신한다 (같은 키 덮어쓰기는 인덱스 불변)."""
        if key not in self.items:
            self._track(key)
        self.items[key] = item

    def _track(self, key: tuple) -> None:
        sks = self._sk_index.setdefault(key[0], [])
        if self.range_key is None:
            if not sks:
                sks.append(None)
            return
        bisect.insort(sks, key[1])

    def _untrack(self, key: tuple) -> None:
        sks = self._sk_index.get(key[0])
        if sks is None:
            return
        if self.range_key is None:
            sks.clear()
        else:
            i = bisect.bisect_left(sks, key[1])
            if i < len(sks) and sks[i] == key[1]:
                del sks[i]
        if not sks:
            self._sk_index.pop(key[0], None)

    def _items_for_pk(self, pk: Any) -> list[dict]:
        """한 pk의 항목을 sk 오름차순으로 (인덱스 순서 그대로 — 정렬 없음)."""
        sks = self._sk_index.get(pk)
        if not sks:
            return []
        if self.range_key is None:
            return [self.items[(pk,)]]
        return [self.items[(pk, sk)] for sk in sks]

    def _sorted_items(self) -> list[dict]:
        """(pk, sk) 오름차순 전 항목 — scan용. pk만 정렬하고 sk는 인덱스 순서를 쓴다."""
        out: list[dict] = []
        for pk in sorted(self._sk_index):
            out.extend(self._items_for_pk(pk))
        return out

    # -- boto3 Table API
    def get_item(self, Key: dict, **kwargs: Any) -> dict:  # noqa: N803
        self._count("get_item")
        item = self.items.get(self._key_tuple(Key))
        return {"Item": copy.deepcopy(item)} if item is not None else {}

    def put_item(self, Item: dict, ConditionExpression: Any = None, **kwargs: Any) -> dict:  # noqa: N803
        self._count("put_item")
        _reject_floats(Item)
        key = self._key_tuple(Item)
        existing = self.items.get(key)
        if not _eval_condition(
            ConditionExpression,
            existing,
            kwargs.get("ExpressionAttributeNames"),
            kwargs.get("ExpressionAttributeValues"),
        ):
            raise _client_error(
                "ConditionalCheckFailedException",
                "The conditional request failed",
                "PutItem",
            )
        self._store(key, copy.deepcopy(Item))
        out: dict[str, Any] = {}
        if kwargs.get("ReturnValues") == "ALL_OLD" and existing is not None:
            out["Attributes"] = copy.deepcopy(existing)
        return out

    def update_item(  # noqa: N803
        self,
        Key: dict,
        UpdateExpression: str,
        ExpressionAttributeNames: dict[str, str] | None = None,
        ExpressionAttributeValues: dict[str, Any] | None = None,
        ConditionExpression: Any = None,
        ReturnValues: str = "NONE",
        **kwargs: Any,
    ) -> dict:
        self._count("update_item")
        _reject_floats(ExpressionAttributeValues or {})
        key = self._key_tuple(Key)
        existing = self.items.get(key)
        if not _eval_condition(
            ConditionExpression, existing, ExpressionAttributeNames, ExpressionAttributeValues
        ):
            raise _client_error(
                "ConditionalCheckFailedException",
                "The conditional request failed",
                "UpdateItem",
            )
        old = copy.deepcopy(existing) if existing is not None else None
        item = copy.deepcopy(existing) if existing is not None else dict(Key)
        upd = _Updater(item, ExpressionAttributeNames, ExpressionAttributeValues)
        upd.apply(UpdateExpression)
        _reject_floats(item)
        self._store(key, item)
        if ReturnValues == "ALL_NEW":
            return {"Attributes": copy.deepcopy(item)}
        if ReturnValues == "ALL_OLD":
            return {"Attributes": copy.deepcopy(old)} if old is not None else {}
        if ReturnValues == "UPDATED_NEW":
            return {"Attributes": copy.deepcopy(upd.touched)}
        if ReturnValues == "UPDATED_OLD":
            attrs = {k: old.get(k) for k in upd.touched if old and k in old}
            return {"Attributes": copy.deepcopy(attrs)}
        return {}

    def delete_item(self, Key: dict, ConditionExpression: Any = None, **kwargs: Any) -> dict:  # noqa: N803
        self._count("delete_item")
        key = self._key_tuple(Key)
        existing = self.items.get(key)
        if not _eval_condition(ConditionExpression, existing):
            raise _client_error(
                "ConditionalCheckFailedException",
                "The conditional request failed",
                "DeleteItem",
            )
        if self.items.pop(key, None) is not None:
            self._untrack(key)
        if kwargs.get("ReturnValues") == "ALL_OLD" and existing is not None:
            return {"Attributes": copy.deepcopy(existing)}
        return {}

    def query(  # noqa: N803
        self,
        KeyConditionExpression: Any = None,
        FilterExpression: Any = None,
        ScanIndexForward: bool = True,
        Limit: int | None = None,
        ExclusiveStartKey: dict | None = None,
        **kwargs: Any,
    ) -> dict:
        self._count("query")
        if KeyConditionExpression is None:
            raise _client_error(
                "ValidationException", "KeyConditionExpression이 필요함", "Query"
            )
        if not isinstance(KeyConditionExpression, ConditionBase):
            raise NotImplementedError("KeyConditionExpression은 boto3 Key() 조건 객체만 지원")
        pk = self._hash_value(KeyConditionExpression)
        # pk 인덱스로 후보를 좁힌 뒤 sk 조건을 평가한다 (전 항목 정렬 없음)
        rows = [it for it in self._items_for_pk(pk) if _eval_cond_obj(KeyConditionExpression, it)]
        if not ScanIndexForward:
            rows.reverse()
        rows = self._after_start_key(rows, ExclusiveStartKey, ScanIndexForward)
        return self._page(rows, Limit, FilterExpression, kwargs)

    def scan(  # noqa: N803
        self,
        FilterExpression: Any = None,
        Limit: int | None = None,
        ExclusiveStartKey: dict | None = None,
        **kwargs: Any,
    ) -> dict:
        self._count("scan")
        rows = self._sorted_items()
        rows = self._after_start_key(rows, ExclusiveStartKey, True, full_key=True)
        return self._page(rows, Limit, FilterExpression, kwargs)

    def batch_writer(self, **kwargs: Any) -> _FakeBatchWriter:
        return _FakeBatchWriter(self)

    # -- query/scan 공통
    def _hash_value(self, cond: ConditionBase) -> Any:
        """KeyConditionExpression의 pk 동등 조건 값 (DynamoDB처럼 없으면 거부)."""
        for leaf in _leaf_conditions(cond):
            expr = leaf.get_expression()
            vals = expr["values"]
            if (
                expr["operator"] == "="
                and isinstance(vals[0], AttributeBase)
                and vals[0].name == self.hash_key
            ):
                return vals[1]
        raise _client_error(
            "ValidationException",
            f"Query에는 {self.hash_key} 동등 조건이 필요함",
            "Query",
        )

    def _after_start_key(
        self, rows: list[dict], start: dict | None, forward: bool, full_key: bool = False
    ) -> list[dict]:
        if not start:
            return rows
        if full_key or self.range_key is None:

            def sort_key(it: dict) -> tuple:
                return self._key_tuple(it)

            bound = self._key_tuple(start)
        else:

            def sort_key(it: dict) -> tuple:
                return (it.get(self.range_key),)

            bound = (start.get(self.range_key),)
        if forward:
            return [it for it in rows if sort_key(it) > bound]
        return [it for it in rows if sort_key(it) < bound]

    def _page(
        self, rows: list[dict], limit: int | None, filter_expr: Any, kwargs: dict
    ) -> dict:
        cap = min(limit, self.page_size) if limit else self.page_size
        page = rows[:cap]
        last = page[-1] if page else None
        # DynamoDB와 동일하게 Limit(읽기 한도)을 먼저 적용하고 FilterExpression을 적용한다.
        if filter_expr is not None:
            page = [
                it
                for it in page
                if _eval_condition(
                    filter_expr,
                    it,
                    kwargs.get("ExpressionAttributeNames"),
                    kwargs.get("ExpressionAttributeValues"),
                )
            ]
        out: dict[str, Any] = {
            "Items": copy.deepcopy(page),
            "Count": len(page),
            "ScannedCount": min(len(rows), cap),
        }
        if last is not None and len(rows) > cap:
            out["LastEvaluatedKey"] = self._key_of(last)
        return out


def _leaf_conditions(cond: ConditionBase) -> list[ConditionBase]:
    expr = cond.get_expression()
    if expr["operator"] in ("AND", "OR", "NOT"):
        out: list[ConditionBase] = []
        for v in expr["values"]:
            out.extend(_leaf_conditions(v))
        return out
    return [cond]


class _FakeBatchWriter:
    """table.batch_writer() 컨텍스트 — put/delete를 버퍼링해 종료 시 flush."""

    def __init__(self, table: FakeTable, flush_amount: int = 25):
        self.table = table
        self.flush_amount = flush_amount
        self.buffer: list[tuple[str, dict]] = []
        self.flushes = 0

    def __enter__(self) -> _FakeBatchWriter:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.flush()

    def put_item(self, Item: dict, **kwargs: Any) -> None:  # noqa: N803
        _reject_floats(Item)
        self.buffer.append(("put", copy.deepcopy(Item)))
        self._maybe_flush()

    def delete_item(self, Key: dict, **kwargs: Any) -> None:  # noqa: N803
        self.buffer.append(("delete", copy.deepcopy(Key)))
        self._maybe_flush()

    def _maybe_flush(self) -> None:
        if len(self.buffer) >= self.flush_amount:
            self.flush()

    def flush(self) -> None:
        if not self.buffer:
            return
        for kind, payload in self.buffer:
            if kind == "put":
                self.table.put_item(Item=payload)
            else:
                self.table.delete_item(Key=payload)
        self.buffer = []
        self.flushes += 1


# ---------------------------------------------------------------- FakeS3


class _FakeBody:
    def __init__(self, data: bytes):
        self._data = data

    def read(self, amt: int | None = None) -> bytes:
        if amt is None:
            data, self._data = self._data, b""
            return data
        data, self._data = self._data[:amt], self._data[amt:]
        return data

    def close(self) -> None:
        self._data = b""


class FakeS3:
    """boto3 s3 client의 부분집합 (put/get/head/delete/list_objects_v2)."""

    def __init__(self, objects: dict[str, bytes] | None = None):
        self.objects: dict[str, dict[str, Any]] = {}
        self.calls: dict[str, int] = {}
        for k, v in (objects or {}).items():
            self.objects[k] = self._record(v)

    def _count(self, op: str) -> None:
        self.calls[op] = self.calls.get(op, 0) + 1

    @staticmethod
    def _record(body: Any, content_type: str | None = None, encoding: str | None = None) -> dict:
        if isinstance(body, str):
            body = body.encode("utf-8")
        elif not isinstance(body, (bytes, bytearray)):
            raise TypeError(f"Body must be bytes or str, got {type(body).__name__}")
        return {
            "Body": bytes(body),
            "ContentType": content_type,
            "ContentEncoding": encoding,
            "LastModified": datetime.now(timezone.utc),
        }

    def put_object(  # noqa: N803
        self,
        Bucket: str,
        Key: str,
        Body: bytes | str = b"",
        ContentType: str | None = None,
        ContentEncoding: str | None = None,
        **kwargs: Any,
    ) -> dict:
        self._count("put_object")
        self.objects[Key] = self._record(Body, ContentType, ContentEncoding)
        return {"ETag": f'"etag-{Key}"'}

    def get_object(self, Bucket: str, Key: str, **kwargs: Any) -> dict:  # noqa: N803
        self._count("get_object")
        rec = self._require(Key, "GetObject")
        out = {
            "Body": _FakeBody(rec["Body"]),
            "ContentLength": len(rec["Body"]),
            "LastModified": rec["LastModified"],
            "ETag": f'"etag-{Key}"',
        }
        for k in ("ContentType", "ContentEncoding"):
            if rec.get(k):
                out[k] = rec[k]
        return out

    def head_object(self, Bucket: str, Key: str, **kwargs: Any) -> dict:  # noqa: N803
        self._count("head_object")
        rec = self._require(Key, "HeadObject")
        return {
            "ContentLength": len(rec["Body"]),
            "LastModified": rec["LastModified"],
            "ETag": f'"etag-{Key}"',
            "ContentType": rec.get("ContentType"),
            "ContentEncoding": rec.get("ContentEncoding"),
        }

    def delete_object(self, Bucket: str, Key: str, **kwargs: Any) -> dict:  # noqa: N803
        self._count("delete_object")
        self.objects.pop(Key, None)
        return {}

    def list_objects_v2(  # noqa: N803
        self, Bucket: str, Prefix: str = "", MaxKeys: int = 1000, **kwargs: Any
    ) -> dict:
        self._count("list_objects_v2")
        keys = sorted(k for k in self.objects if k.startswith(Prefix))[:MaxKeys]
        out: dict[str, Any] = {
            "KeyCount": len(keys),
            "IsTruncated": False,
            "Prefix": Prefix,
            "Name": Bucket,
        }
        if keys:  # 실제 S3는 결과가 없으면 Contents 키를 아예 내려주지 않는다
            out["Contents"] = [
                {
                    "Key": k,
                    "Size": len(self.objects[k]["Body"]),
                    "LastModified": self.objects[k]["LastModified"],
                    "ETag": f'"etag-{k}"',
                }
                for k in keys
            ]
        return out

    def _require(self, key: str, op: str) -> dict:
        rec = self.objects.get(key)
        if rec is None:
            raise _client_error("NoSuchKey", f"The specified key does not exist: {key}", op)
        return rec
