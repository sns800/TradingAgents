"""재개 가능한 분석 실행을 위한 LangGraph 체크포인트(checkpoint) 지원 모듈.

[모듈 개요 - 초보자용]
체크포인트란 그래프(graph)가 노드(node) 하나를 끝낼 때마다 진행 상태를
SQLite 데이터베이스에 저장해 두는 기능입니다. 실행 도중 프로그램이 죽어도
같은 티커(ticker, 종목 코드)+날짜로 다시 실행하면 마지막으로 성공한 단계부터
이어서 진행할 수 있습니다. trading_graph.py의 propagate()가
``checkpoint_enabled`` 설정이 켜져 있을 때 이 모듈을 사용합니다.

티커별로 SQLite DB 파일을 따로 두어, 여러 티커를 동시에 실행해도
파일 잠금 경합이 생기지 않도록 합니다.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from tradingagents.dataflows.utils import safe_ticker_component


def _db_path(data_dir: str | Path, ticker: str) -> Path:
    """해당 티커의 SQLite 체크포인트 DB 경로를 반환한다."""
    # checkpoints 디렉터리를 벗어날 수 있는 티커 값(예: "../evil")은 거부합니다.
    safe = safe_ticker_component(ticker).upper()
    p = Path(data_dir) / "checkpoints"
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{safe}.db"


def thread_id(ticker: str, date: str, signature: str = "") -> str:
    """티커+날짜 조합에 대한 결정적(deterministic) 스레드 ID를 생성한다.

    ``signature``에는 그래프 모양에 영향을 주는 실행 옵션(선택된 애널리스트,
    토론 라운드 수 등)이 담깁니다. 이를 ID에 섞어 넣으면, 다른 그래프 구성으로
    재개(resume)할 때 이전 체크포인트를 잘못 재사용하는 일을 막을 수 있습니다
    (#1089). signature를 생략하면 기존(legacy) ID가 유지됩니다.
    """
    base = f"{ticker.upper()}:{date}"
    if signature:
        base = f"{base}:{signature}"
    return hashlib.sha256(base.encode()).hexdigest()[:16]


@contextmanager
def get_checkpointer(data_dir: str | Path, ticker: str) -> Generator[SqliteSaver, None, None]:
    """티커별 DB에 연결된 SqliteSaver를 내어주는 컨텍스트 매니저(context manager)."""
    db = _db_path(data_dir, ticker)
    conn = sqlite3.connect(str(db), check_same_thread=False)
    try:
        saver = SqliteSaver(conn)
        saver.setup()
        yield saver
    finally:
        conn.close()


def has_checkpoint(data_dir: str | Path, ticker: str, date: str, signature: str = "") -> bool:
    """티커+날짜에 대해 재개 가능한 체크포인트가 존재하는지 확인한다."""
    return checkpoint_step(data_dir, ticker, date, signature) is not None


def checkpoint_step(data_dir: str | Path, ticker: str, date: str, signature: str = "") -> int | None:
    """가장 최근 체크포인트의 단계(step) 번호를 반환하고, 없으면 None을 반환한다."""
    db = _db_path(data_dir, ticker)
    if not db.exists():
        return None
    tid = thread_id(ticker, date, signature)
    with get_checkpointer(data_dir, ticker) as saver:
        config = {"configurable": {"thread_id": tid}}
        cp = saver.get_tuple(config)
        if cp is None:
            return None
        return cp.metadata.get("step")


def clear_all_checkpoints(data_dir: str | Path) -> int:
    """모든 체크포인트 DB 파일을 삭제한다. 삭제한 파일 개수를 반환한다."""
    cp_dir = Path(data_dir) / "checkpoints"
    if not cp_dir.exists():
        return 0
    dbs = list(cp_dir.glob("*.db"))
    for db in dbs:
        db.unlink()
    return len(dbs)


def clear_checkpoint(data_dir: str | Path, ticker: str, date: str, signature: str = "") -> None:
    """특정 티커+날짜의 체크포인트를, 해당 스레드의 행(row)만 지워서 제거한다."""
    db = _db_path(data_dir, ticker)
    if not db.exists():
        return
    tid = thread_id(ticker, date, signature)
    conn = sqlite3.connect(str(db))
    try:
        for table in ("writes", "checkpoints"):
            conn.execute(f"DELETE FROM {table} WHERE thread_id = ?", (tid,))
        conn.commit()
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()
