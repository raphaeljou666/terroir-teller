"""`agent` 模組的單元測試。

全部離線執行，不打 OpenAI API：`tools.TOOL_DISPATCH` 用 monkeypatch 替換掉底層工具，用
假的 tool_call 物件（模擬 OpenAI SDK 回傳的 `ChatCompletionMessageToolCall` 形狀）驅動
迴圈的邏輯分支。測試重點放在最攸關可信度的地方——拒答清單第 8 項的程式層硬擋，以及 CLI
參數的互斥規則。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

import agent


@dataclass
class _FakeFunction:
    name: str
    arguments: str


@dataclass
class _FakeToolCall:
    id: str
    function: _FakeFunction
    type: str = "function"


def _make_tool_call(name: str, arguments: dict[str, Any], call_id: str = "call_1") -> _FakeToolCall:
    return _FakeToolCall(id=call_id, function=_FakeFunction(name=name, arguments=json.dumps(arguments)))


# --- _dispatch_tool_call --------------------------------------------------------------


def test_dispatch_tool_call會透過TOOL_DISPATCH執行並回傳結果(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        agent.tools.TOOL_DISPATCH, "check_region_validity",
        lambda region_name: {"valid": True, "region_canonical": region_name, "region_zh": "測試"},
    )
    tool_call = _make_tool_call("check_region_validity", {"region_name": "Bordeaux"})

    name, arguments, result = agent._dispatch_tool_call(tool_call)

    assert name == "check_region_validity"
    assert arguments == {"region_name": "Bordeaux"}
    assert result == {"valid": True, "region_canonical": "Bordeaux", "region_zh": "測試"}


def test_dispatch_tool_call遇到未知工具名稱回傳錯誤而不拋例外() -> None:
    tool_call = _make_tool_call("not_a_real_tool", {})
    name, _arguments, result = agent._dispatch_tool_call(tool_call)
    assert name == "not_a_real_tool"
    assert result["error"] is True
    assert result["error_type"] == "unknown_tool"


def test_dispatch_tool_call參數不是合法JSON時改用空dict呼叫(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _spy(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"valid": True, "region_canonical": "x", "region_zh": "x"}

    monkeypatch.setitem(agent.tools.TOOL_DISPATCH, "check_region_validity", _spy)
    tool_call = _FakeToolCall(id="call_1", function=_FakeFunction(name="check_region_validity", arguments="{not json"))

    agent._dispatch_tool_call(tool_call)

    assert captured == {}


# --- _update_gathered_data -------------------------------------------------------------


def test_update_gathered_data寫入酒標辨識結果() -> None:
    gathered = agent.GatheredData()
    result = {"region": "Bordeaux", "winery": "Test", "vintage": 2019, "grape": "Merlot"}
    agent._update_gathered_data(gathered, "recognize_wine_label", result)
    assert gathered.label_info == result


def test_update_gathered_data產區合法時填入region欄位() -> None:
    gathered = agent.GatheredData()
    result = {"valid": True, "region_canonical": "Bordeaux", "region_zh": "波爾多"}
    agent._update_gathered_data(gathered, "check_region_validity", result)
    assert gathered.region_validity == result
    assert gathered.region_canonical == "Bordeaux"
    assert gathered.region_zh == "波爾多"


def test_update_gathered_data產區不合法時只記錄不填region欄位() -> None:
    gathered = agent.GatheredData()
    result = {"valid": False, "reason": "本系統目前不涵蓋「Santorini」這個產區。"}
    agent._update_gathered_data(gathered, "check_region_validity", result)
    assert gathered.region_validity == result
    assert gathered.region_canonical is None


def test_update_gathered_data寫入氣候距平並補上vintage_year() -> None:
    gathered = agent.GatheredData()
    result = {"vintage_year": 2019, "summary": "..."}
    agent._update_gathered_data(gathered, "query_climate_anomaly", result)
    assert gathered.anomaly == result
    assert gathered.vintage_year == 2019


def test_update_gathered_data累加知識檢索結果() -> None:
    gathered = agent.GatheredData()
    agent._update_gathered_data(gathered, "query_climate_knowledge", {"results": [{"id": "a"}]})
    agent._update_gathered_data(gathered, "query_climate_knowledge", {"results": [{"id": "b"}]})
    assert [hit["id"] for hit in gathered.knowledge_hits] == ["a", "b"]


def test_update_gathered_data工具出錯時不寫入對應欄位() -> None:
    gathered = agent.GatheredData()
    error_result = {"error": True, "error_type": "climate_anomaly", "user_message": "..."}
    agent._update_gathered_data(gathered, "query_climate_anomaly", error_result)
    assert gathered.anomaly is None


# --- _process_tool_calls：拒答清單第 8 項的硬擋 ------------------------------------------


def test_process_tool_calls偵測到產區不合法時回傳True並中止(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        agent.tools.TOOL_DISPATCH, "check_region_validity",
        lambda region_name: {"valid": False, "reason": f"本系統目前不涵蓋「{region_name}」這個產區。"},
    )
    gathered = agent.GatheredData()
    messages: list[dict[str, Any]] = []
    tool_call = _make_tool_call("check_region_validity", {"region_name": "Santorini"})

    region_not_covered = agent._process_tool_calls([tool_call], gathered, messages)

    assert region_not_covered is True
    assert gathered.region_validity["valid"] is False
    assert messages[0]["role"] == "tool"
    assert messages[0]["tool_call_id"] == tool_call.id


def test_process_tool_calls產區合法時回傳False(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        agent.tools.TOOL_DISPATCH, "check_region_validity",
        lambda region_name: {"valid": True, "region_canonical": region_name, "region_zh": "波爾多"},
    )
    gathered = agent.GatheredData()
    tool_call = _make_tool_call("check_region_validity", {"region_name": "Bordeaux"})

    assert agent._process_tool_calls([tool_call], gathered, []) is False


# --- _label_missing_region --------------------------------------------------------------


def test_label_missing_region酒標沒有產區時回傳True() -> None:
    gathered = agent.GatheredData(
        label_info={"region": None, "winery": "X", "vintage": None, "grape": None}
    )
    assert agent._label_missing_region(gathered) is True


def test_label_missing_region只缺年份時回傳False() -> None:
    gathered = agent.GatheredData(
        label_info={"region": "Bordeaux", "winery": None, "vintage": None, "grape": None}
    )
    assert agent._label_missing_region(gathered) is False


def test_label_missing_region還沒辨識酒標時回傳False() -> None:
    assert agent._label_missing_region(agent.GatheredData()) is False


# --- _message_to_dict --------------------------------------------------------------------


def test_message_to_dict保留tool_calls結構() -> None:
    @dataclass
    class _FakeMessage:
        role: str
        content: str | None
        tool_calls: list[Any]

    tool_call = _make_tool_call("check_region_validity", {"region_name": "Bordeaux"})
    message = _FakeMessage(role="assistant", content=None, tool_calls=[tool_call])

    payload = agent._message_to_dict(message)

    assert payload["role"] == "assistant"
    assert payload["tool_calls"][0]["id"] == tool_call.id
    assert payload["tool_calls"][0]["function"]["name"] == "check_region_validity"


def test_message_to_dict沒有tool_calls時不含該欄位() -> None:
    @dataclass
    class _FakeMessage:
        role: str
        content: str | None
        tool_calls: list[Any] | None

    message = _FakeMessage(role="assistant", content="done", tool_calls=None)
    payload = agent._message_to_dict(message)
    assert "tool_calls" not in payload


# --- CLI 參數驗證 -------------------------------------------------------------------------


def test_validate_args同時給image與region會報錯退出碼2() -> None:
    parser = agent.build_parser()
    args = parser.parse_args(["--image", "x.jpg", "--region", "Bordeaux", "--year", "2019"])
    with pytest.raises(SystemExit) as excinfo:
        agent._validate_args(parser, args)
    assert excinfo.value.code == 2


def test_validate_args只給region沒給year會報錯() -> None:
    parser = agent.build_parser()
    args = parser.parse_args(["--region", "Bordeaux"])
    with pytest.raises(SystemExit) as excinfo:
        agent._validate_args(parser, args)
    assert excinfo.value.code == 2


def test_validate_args只給image時通過驗證() -> None:
    parser = agent.build_parser()
    args = parser.parse_args(["--image", "x.jpg"])
    agent._validate_args(parser, args)


def test_validate_args同時給region與year時通過驗證() -> None:
    parser = agent.build_parser()
    args = parser.parse_args(["--region", "Bordeaux", "--year", "2019"])
    agent._validate_args(parser, args)


def test_main無任何參數時列印說明並回傳2(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = agent.main([])
    assert exit_code == 2
    assert "usage" in capsys.readouterr().out.lower()
