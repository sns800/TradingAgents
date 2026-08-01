"""[모듈 개요] 종목 카탈로그 파서(webui/catalog/parsers.py) 테스트.

픽스처는 실제 거래소 공개 파일(NASDAQ Trader symbol directory, KRX KIND
상장법인목록, JPX 상장종목일람)에서 잘라낸 고정본이며, 외부 네트워크 호출 없이
파싱·인코딩·야후 접미사 매핑·제외 규칙을 검증합니다.
"""

import importlib.util
import sys
import unittest
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "catalog"


def _load_parsers():
    """webui/catalog은 패키지가 아니므로(스크립트 디렉토리) 파일 경로로 로드한다."""
    spec = importlib.util.spec_from_file_location(
        "catalog_parsers", REPO_ROOT / "webui" / "catalog" / "parsers.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("catalog_parsers", module)
    spec.loader.exec_module(module)
    return module


parsers = _load_parsers()

ITEM_KEYS = {
    "ticker", "name", "market", "sector", "industry", "price", "currency", "market_cap",
}


def _by_ticker(items):
    return {item["ticker"]: item for item in items}


@pytest.mark.unit
class TestParseUS(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.items = parsers.parse_us(
            (FIXTURES / "nasdaqlisted_sample.txt").read_bytes(),
            (FIXTURES / "otherlisted_sample.txt").read_bytes(),
        )
        cls.index = _by_ticker(cls.items)

    def test_item_schema(self):
        """모든 item이 계약 스키마의 키를 정확히 갖는지 검증하는 테스트."""
        for item in self.items:
            self.assertEqual(set(item), ITEM_KEYS)

    def test_nasdaq_common_stock_included(self):
        """나스닥 보통주가 심볼 그대로(NASDAQ 시장명, USD)로 들어오는지 검증하는 테스트."""
        apple = self.index["AAPL"]
        self.assertEqual(apple["name"], "Apple Inc. - Common Stock")
        self.assertEqual(apple["market"], "NASDAQ")
        self.assertEqual(apple["currency"], "USD")
        # US 소스에는 업종 정보가 없고 시세 보강도 하지 않으므로 sector는 None
        self.assertIsNone(apple["sector"])
        self.assertIsNone(apple["price"])
        self.assertIsNone(apple["market_cap"])

    def test_class_share_dot_to_dash(self):
        """클래스 주식의 점 표기가 야후 대시 표기로 바뀌는지 검증하는 테스트 (BRK.B -> BRK-B)."""
        self.assertIn("BRK-A", self.index)
        self.assertIn("BRK-B", self.index)
        self.assertNotIn("BRK.B", self.index)
        self.assertEqual(self.index["BRK-B"]["market"], "NYSE")

    def test_test_issues_excluded(self):
        """Test Issue=Y 종목이 두 파일 모두에서 제외되는지 검증하는 테스트."""
        self.assertNotIn("ZAZZT", self.index)  # nasdaqlisted의 테스트 종목
        self.assertNotIn("ATEST", self.index)  # otherlisted의 테스트 종목

    def test_etf_excluded(self):
        """ETF=Y 종목이 두 파일 모두에서 제외되는지 검증하는 테스트."""
        self.assertNotIn("AAAP", self.index)  # nasdaqlisted의 ETF
        self.assertNotIn("SPY", self.index)   # otherlisted의 ETF
        self.assertNotIn("BRKC", self.index)  # otherlisted의 ETF

    def test_preferred_share_excluded(self):
        """'$' 우선주(야후 표기 비표준)가 제외되는지 검증하는 테스트."""
        self.assertFalse(any("$" in t or "ABR" in t for t in self.index))

    def test_exchange_code_mapping(self):
        """otherlisted의 Exchange 코드가 거래소 이름으로 매핑되는지 검증하는 테스트."""
        self.assertEqual(self.index["A"]["market"], "NYSE")  # Agilent, Exchange=N
        self.assertEqual(self.index["IBM"]["market"], "NYSE")

    def test_file_creation_time_line_skipped(self):
        """마지막 줄 'File Creation Time: ...'이 종목으로 파싱되지 않는지 검증하는 테스트."""
        self.assertFalse(any(t.startswith("FILE") for t in self.index))
        # 픽스처 기대 종목 수: nasdaq(AAPL, MSFT) + other(A, BRK.A, BRK.B, IBM)
        self.assertEqual(len(self.items), 6)


@pytest.mark.unit
class TestParseKR(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 픽스처는 KIND 원본에서 잘라낸 EUC-KR HTML (유가/코스닥/코넥스 각 1종목)
        cls.items = parsers.parse_kr((FIXTURES / "krx_corplist_sample.xls").read_bytes())
        cls.index = _by_ticker(cls.items)

    def test_item_schema(self):
        for item in self.items:
            self.assertEqual(set(item), ITEM_KEYS)

    def test_euc_kr_decoding_and_kospi_suffix(self):
        """EUC-KR 한글이 깨지지 않고 유가증권 종목에 .KS가 붙는지 검증하는 테스트."""
        samsung = self.index["005930.KS"]
        self.assertEqual(samsung["name"], "삼성전자")
        self.assertEqual(samsung["market"], "KOSPI")
        self.assertEqual(samsung["currency"], "KRW")
        # 업종 컬럼이 sector로 들어온다
        self.assertEqual(samsung["sector"], "통신 및 방송 장비 제조업")

    def test_kosdaq_suffix_and_alnum_code(self):
        """코스닥 종목에 .KQ가 붙고 신형 영숫자 종목코드가 유지되는지 검증하는 테스트."""
        neovue = self.index["0218L0.KQ"]
        self.assertEqual(neovue["name"], "네오뷰")
        self.assertEqual(neovue["market"], "KOSDAQ")

    def test_konex_excluded(self):
        """코넥스 종목(야후 미지원)이 제외되는지 검증하는 테스트."""
        self.assertEqual(len(self.items), 2)  # 픽스처의 코넥스 1종목(나노솔루션)은 제외
        self.assertFalse(any(item["name"] == "나노솔루션" for item in self.items))

    def test_duplicate_rows_deduped(self):
        """KIND 원본의 중복 행(실측 37건)이 하나로 합쳐지는지 검증하는 테스트."""
        row = "<tr><td>중복사</td><td>유가</td><td>439260</td><td>선박 및 보트 건조업</td></tr>"
        html = (
            "<html><body><table>"
            "<tr><th>회사명</th><th>시장구분</th><th>종목코드</th><th>업종</th></tr>"
            f"{row}{row}"
            "</table></body></html>"
        ).encode("euc-kr")
        items = parsers.parse_kr(html)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["ticker"], "439260.KS")

    def test_code_zero_padding(self):
        """짧은 종목코드가 6자리로 zero-pad 되는지 검증하는 테스트."""
        # 픽스처 HTML의 코드는 텍스트라 0이 보존되지만, 파서는 방어적으로 zfill한다.
        html = (
            "<html><body><table>"
            "<tr><th>회사명</th><th>시장구분</th><th>종목코드</th><th>업종</th></tr>"
            "<tr><td>테스트사</td><td>유가</td><td>5930</td><td>전기·전자</td></tr>"
            "</table></body></html>"
        ).encode("euc-kr")
        items = parsers.parse_kr(html)
        self.assertEqual(items[0]["ticker"], "005930.KS")
        self.assertEqual(items[0]["sector"], "전기·전자")


@pytest.mark.unit
class TestParseJP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pytest.importorskip("xlrd")  # 레거시 .xls 픽스처를 읽으려면 xlrd 필요
        # 픽스처는 JPX data_j.xls 원본에서 잘라낸 행들로 만든 실제 .xls 파일
        cls.items = parsers.parse_jp((FIXTURES / "jpx_data_j_sample.xls").read_bytes())
        cls.index = _by_ticker(cls.items)

    def test_item_schema(self):
        for item in self.items:
            self.assertEqual(set(item), ITEM_KEYS)

    def test_tokyo_suffix_and_sector(self):
        """4자리 코드에 .T가 붙고 33업종 구분이 sector로 들어오는지 검증하는 테스트."""
        toyota = self.index["7203.T"]
        self.assertEqual(toyota["name"], "トヨタ自動車")
        self.assertEqual(toyota["market"], "Prime")
        self.assertEqual(toyota["sector"], "輸送用機器")
        self.assertEqual(toyota["currency"], "JPY")

    def test_market_segment_names(self):
        """プライム/スタンダード/グロース가 Prime/Standard/Growth로 매핑되는지 검증하는 테스트."""
        self.assertEqual(self.index["1301.T"]["market"], "Prime")
        self.assertEqual(self.index["1376.T"]["market"], "Standard")
        self.assertEqual(self.index["130A.T"]["market"], "Growth")

    def test_alpha_code_kept(self):
        """영문 포함 신형 코드(130A)가 그대로 .T 티커가 되는지 검증하는 테스트."""
        self.assertIn("130A.T", self.index)

    def test_non_common_stock_excluded(self):
        """ETF·REIT·PRO Market·出資証券이 제외되고 외국주식은 포함되는지 검증하는 테스트."""
        self.assertNotIn("1305.T", self.index)  # ETF・ETN
        self.assertNotIn("2971.T", self.index)  # REIT
        self.assertNotIn("131A.T", self.index)  # PRO Market
        self.assertNotIn("8301.T", self.index)  # 出資証券 (일본은행)
        self.assertIn("1773.T", self.index)     # プライム（外国株式）
        self.assertEqual(len(self.items), 5)


if __name__ == "__main__":
    unittest.main()
