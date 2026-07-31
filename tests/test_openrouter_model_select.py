"""[모듈 개요] OpenRouter 모델 선택 UI를 검증하는 테스트: 프롬프트에 모드 라벨이
표시되고(#1000), 필수 프롬프트는 취소 시 깔끔하게 종료되며, 출력 언어 프롬프트는
취소 시 영어(English)를 기본값으로 쓰고, OpenRouter 목록은 최신순으로 정렬된다.
"""

from unittest import mock

import pytest

from cli import utils


def _asks(value):
    return mock.Mock(ask=mock.Mock(return_value=value))


@pytest.mark.unit
class TestOpenRouterPromptLabel:
    @pytest.mark.parametrize("mode,label", [("quick", "Quick-Thinking"), ("deep", "Deep-Thinking")])
    def test_prompt_states_the_mode(self, mode, label):
        """모델 선택 프롬프트 문구에 현재 모드(Quick/Deep) 라벨이 표시되는지 검증하는 테스트."""
        captured = {}

        def fake_select(message, **kwargs):
            captured["message"] = message
            return _asks("openrouter/some-model")

        with mock.patch.object(utils, "_fetch_openrouter_models",
                               return_value=[("Some Model", "openrouter/some-model")]), \
             mock.patch.object(utils.questionary, "select", side_effect=fake_select):
            out = utils.select_openrouter_model(mode)

        assert label in captured["message"]
        assert out == "openrouter/some-model"


@pytest.mark.unit
class TestOpenRouterLatestFirst:
    def test_models_sorted_newest_first(self):
        """OpenRouter 모델 목록이 생성 시각 기준 최신순으로 정렬되는지 검증하는 테스트."""
        payload = {"data": [
            {"id": "old/model", "name": "Old", "created": 1000},
            {"id": "new/model", "name": "New", "created": 3000},
            {"id": "mid/model", "name": "Mid", "created": 2000},
        ]}
        resp = mock.Mock()
        resp.json.return_value = payload
        resp.raise_for_status = mock.Mock()
        with mock.patch("requests.get", return_value=resp):
            out = utils._fetch_openrouter_models()
        assert [mid for _, mid in out] == ["new/model", "mid/model", "old/model"]


@pytest.mark.unit
class TestMainstreamFilter:
    def test_dropdown_prefers_mainstream_over_niche(self):
        """드롭다운이 비주류(niche) 네임스페이스 대신 주류(mainstream) 모델을 우선하는지 검증하는 테스트."""
        # _fetch는 최신순으로 반환한다; 요약 목록(shortlist)은 비주류 네임스페이스를 제외해야 한다.
        models = [
            ("Fusion", "openrouter/fusion"),
            ("Niche", "nex-agi/nex-n2-pro:free"),
            ("Claude", "anthropic/claude-x"),
            ("GPT", "openai/gpt-x"),
        ]
        captured = {}

        def fake_select(message, **kwargs):
            captured["values"] = [c.value for c in kwargs["choices"]]
            return _asks("anthropic/claude-x")

        with mock.patch.object(utils, "_fetch_openrouter_models", return_value=models), \
             mock.patch.object(utils.questionary, "select", side_effect=fake_select):
            utils.select_openrouter_model("quick")

        assert "anthropic/claude-x" in captured["values"]
        assert "openai/gpt-x" in captured["values"]
        assert "openrouter/fusion" not in captured["values"]
        assert "nex-agi/nex-n2-pro:free" not in captured["values"]
        assert "custom" in captured["values"]  # 탈출구(escape hatch)는 유지됨

    def test_falls_back_to_all_when_no_mainstream(self):
        """주류 모델이 하나도 없으면 전체 목록으로 폴백(fallback)하는지 검증하는 테스트."""
        models = [("Niche", "nex-agi/x"), ("Other", "thedrummer/y")]
        captured = {}

        def fake_select(message, **kwargs):
            captured["values"] = [c.value for c in kwargs["choices"]]
            return _asks("nex-agi/x")

        with mock.patch.object(utils, "_fetch_openrouter_models", return_value=models), \
             mock.patch.object(utils.questionary, "select", side_effect=fake_select):
            utils.select_openrouter_model("deep")

        assert "nex-agi/x" in captured["values"]  # 폴백 덕분에 목록이 계속 사용 가능함


@pytest.mark.unit
class TestCancelExitsCleanly:
    def test_dropdown_cancel_exits(self):
        """모델 드롭다운을 취소하면 프로그램이 깔끔하게 종료(SystemExit)되는지 검증하는 테스트."""
        with mock.patch.object(utils, "_fetch_openrouter_models", return_value=[]), \
             mock.patch.object(utils.questionary, "select", return_value=_asks(None)), \
             pytest.raises(SystemExit):
            utils.select_openrouter_model("quick")

    def test_custom_id_cancel_exits(self):
        """사용자 지정 모델 ID 입력을 취소해도 깔끔하게 종료되는지 검증하는 테스트."""
        with mock.patch.object(utils, "_fetch_openrouter_models", return_value=[]), \
             mock.patch.object(utils.questionary, "select", return_value=_asks("custom")), \
             mock.patch.object(utils.questionary, "text", return_value=_asks(None)), \
             pytest.raises(SystemExit):
            utils.select_openrouter_model("deep")

    def test_prompt_custom_model_id_cancel_exits(self):
        """_prompt_custom_model_id에서 취소하면 깔끔하게 종료되는지 검증하는 테스트."""
        with mock.patch.object(utils.questionary, "text", return_value=_asks(None)), \
             pytest.raises(SystemExit):
            utils._prompt_custom_model_id()


@pytest.mark.unit
class TestLanguageDefaultsToKorean:
    # 한글화 포크: 기본 출력 언어가 English에서 Korean으로 변경됨
    def test_select_cancel_defaults_korean(self):
        """출력 언어 선택을 취소하면 한국어(Korean)가 기본값이 되는지 검증하는 테스트."""
        with mock.patch.object(utils.questionary, "select", return_value=_asks(None)):
            assert utils.ask_output_language() == "Korean"

    def test_custom_language_cancel_defaults_korean(self):
        """사용자 지정 언어 입력을 취소해도 한국어가 기본값이 되는지 검증하는 테스트."""
        with mock.patch.object(utils.questionary, "select", return_value=_asks("custom")), \
             mock.patch.object(utils.questionary, "text", return_value=_asks(None)):
            assert utils.ask_output_language() == "Korean"
