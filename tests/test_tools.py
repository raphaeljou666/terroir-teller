"""`src.tools` 的測試。

`tools.py` 本身沒有直接的外部呼叫（HTTP／OpenAI API），所有 5 個 dispatch 函式的外部依賴
都在底層模組（`climate`／`retrieval`／`vision`）裡，這裡一律用 monkeypatch 隔離，全部離線
執行，不需要任何 API Key。`check_region_validity` 例外——它只讀本機 `data/regions.json`，
不需要 monkeypatch 就能真實測試。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from src import climate, retrieval, tools, vision

# --- Schema 結構驗證（離線） ----------------------------------------------------


@pytest.mark.parametrize("schema", tools.ALL_TOOL_SCHEMAS, ids=lambda s: s["function"]["name"])
def test_每個工具的schema符合OpenAI_function_calling規範(schema: dict[str, Any]) -> None:
    assert schema["type"] == "function", f"type 應為 'function'，實際：{schema.get('type')!r}"
    function = schema["function"]

    name = function["name"]
    assert isinstance(name, str) and name, f"name 應為非空字串，實際：{name!r}"
    assert name.replace("_", "").isalnum() and name == name.lower(), (
        f"name 應為 snake_case，實際：{name!r}"
    )

    description = function["description"]
    assert isinstance(description, str) and description.strip(), (
        f"description 應為非空字串，實際：{description!r}"
    )

    parameters = function["parameters"]
    assert parameters["type"] == "object", (
        f"parameters.type 應為 'object'，實際：{parameters.get('type')!r}"
    )
    properties = parameters["properties"]
    assert isinstance(properties, dict), f"properties 應為 dict，實際：{type(properties)!r}"

    required = parameters.get("required", [])
    assert isinstance(required, list), f"required 應為 list，實際：{type(required)!r}"
    for key in required:
        assert key in properties, f"required 欄位 {key!r} 不在 properties 內：{list(properties)}"


def test_五個工具名稱與TOOL_DISPATCH的key一致() -> None:
    schema_names = {schema["function"]["name"] for schema in tools.ALL_TOOL_SCHEMAS}
    dispatch_names = set(tools.TOOL_DISPATCH)
    assert schema_names == dispatch_names, (
        f"schema 名稱與 dispatch 註冊表不一致：schema={schema_names}, dispatch={dispatch_names}"
    )
    assert len(tools.ALL_TOOL_SCHEMAS) == 5, f"預期定案為 5 個工具，實際：{len(tools.ALL_TOOL_SCHEMAS)}"


# --- dispatch_query_climate_anomaly（離線，monkeypatch climate） -----------------


def test_dispatch_query_climate_anomaly_呼叫底層三個climate函式並回傳可序列化dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    fake_season = object()
    fake_baseline = object()

    def fake_fetch_season_climate(region_name: str, vintage_year: int) -> object:
        calls.append(("fetch_season_climate", region_name, vintage_year))
        return fake_season

    def fake_get_baseline(region_name: str) -> object:
        calls.append(("get_baseline", region_name))
        return fake_baseline

    class _FakeMetricAnomaly:
        def __init__(self) -> None:
            self.pct_anomaly = 12.0

    class _FakeAnomaly:
        region_canonical = "Bordeaux"
        vintage_year = 2019
        gdd = _FakeMetricAnomaly()
        season_precipitation = _FakeMetricAnomaly()
        pre_harvest_precipitation = _FakeMetricAnomaly()

        def to_dict(self) -> dict[str, Any]:
            return {"region_canonical": "Bordeaux", "vintage_year": 2019}

    def fake_compute_climate_anomaly(season: object, baseline: object) -> _FakeAnomaly:
        calls.append(("compute_climate_anomaly", season, baseline))
        return _FakeAnomaly()

    monkeypatch.setattr(climate, "fetch_season_climate", fake_fetch_season_climate)
    monkeypatch.setattr(climate, "get_baseline", fake_get_baseline)
    monkeypatch.setattr(climate, "compute_climate_anomaly", fake_compute_climate_anomaly)
    monkeypatch.setattr(climate, "format_anomaly_summary", lambda anomaly: "摘要文字")

    result = tools.dispatch_query_climate_anomaly("Bordeaux", 2019)

    assert calls == [
        ("fetch_season_climate", "Bordeaux", 2019),
        ("get_baseline", "Bordeaux"),
        ("compute_climate_anomaly", fake_season, fake_baseline),
    ], f"底層三個函式的呼叫順序或參數不符，實際呼叫紀錄：{calls}"
    assert result["region_canonical"] == "Bordeaux", f"應保留 to_dict() 的結果，實際：{result}"
    assert result["summary"] == "摘要文字", f"應附上 format_anomaly_summary 的結果，實際：{result}"
    # T-16 後續修正：方向與確定性查詢字串要一起附上，agent 迴圈才拿得到。
    assert result["temperature_direction"] == "warmer", f"GDD +12% 應判為偏暖，實際：{result}"
    assert "偏暖" in result["direction_query"]
    json.dumps(result)  # 確認回傳結果可 JSON 序列化


def test_dispatch_query_climate_anomaly_捕捉ClimateDataError並回傳error_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_fetch_season_climate(region_name: str, vintage_year: int) -> None:
        raise climate.RegionNotFoundError("查無此產區", "技術細節：region_name 不在清單內")

    monkeypatch.setattr(climate, "fetch_season_climate", fake_fetch_season_climate)

    result = tools.dispatch_query_climate_anomaly("Santorini", 2019)

    assert result == {
        "error": True,
        "error_type": "climate_anomaly",
        "user_message": "查無此產區",
    }, f"應捕捉例外並回傳 error dict，不拋出，實際：{result}"


# --- dispatch_query_climate_knowledge（離線，monkeypatch retrieval） -------------


def test_dispatch_query_climate_knowledge_呼叫底層函式並回傳results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_hits = [{"id": "rule_warm_dry_01", "document": "...", "metadata": {}, "distance": 0.1}]

    def fake_query_climate_knowledge(
        query_text: str,
        n_results: int,
        region_canonical: str | None,
        temperature_direction: str | None = None,
    ) -> list[dict[str, Any]]:
        assert query_text == "偏暖偏乾"
        assert n_results == 5
        assert region_canonical is None
        assert temperature_direction is None  # 沒指定方向時不施加過濾
        return fake_hits

    monkeypatch.setattr(retrieval, "query_climate_knowledge", fake_query_climate_knowledge)

    result = tools.dispatch_query_climate_knowledge("偏暖偏乾")

    assert result == {"results": fake_hits}, f"應原樣包成 results，實際：{result}"
    json.dumps(result)


def test_dispatch_query_climate_knowledge_把方向參數往下傳給檢索層(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_query_climate_knowledge(
        query_text: str,
        n_results: int,
        region_canonical: str | None,
        temperature_direction: str | None = None,
    ) -> list[dict[str, Any]]:
        captured["temperature_direction"] = temperature_direction
        return []

    monkeypatch.setattr(retrieval, "query_climate_knowledge", fake_query_climate_knowledge)

    tools.dispatch_query_climate_knowledge("生長季偏涼", temperature_direction="cooler")

    assert captured["temperature_direction"] == "cooler"


def test_方向參數刻意不出現在tool_schema裡() -> None:
    """LLM 不該有機會自己決定冷暖方向，schema 沒宣告它就送不出來。"""
    properties = tools.CLIMATE_KNOWLEDGE_SCHEMA["function"]["parameters"]["properties"]
    assert "temperature_direction" not in properties
    assert tools.CLIMATE_KNOWLEDGE_SCHEMA["function"]["parameters"]["additionalProperties"] is False


def test_dispatch_query_climate_knowledge_捕捉RetrievalError並回傳error_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_query_climate_knowledge(*args: Any, **kwargs: Any) -> None:
        raise retrieval.RetrievalError("知識檢索暫時無法使用", "OpenAIError: rate limited")

    monkeypatch.setattr(retrieval, "query_climate_knowledge", fake_query_climate_knowledge)

    result = tools.dispatch_query_climate_knowledge("偏暖偏乾")

    assert result == {
        "error": True,
        "error_type": "climate_knowledge_retrieval",
        "user_message": "知識檢索暫時無法使用",
    }, f"應捕捉例外並回傳 error dict，不拋出，實際：{result}"


# --- dispatch_query_terroir_knowledge（離線，monkeypatch retrieval） -------------


def test_dispatch_query_terroir_knowledge_呼叫query_all_knowledge並回傳results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_hits = [{"id": "bordeaux_05_comparison", "document": "...", "metadata": {}, "distance": 0.2}]

    def fake_query_all_knowledge(
        query_text: str, n_results: int, region_canonical: str | None
    ) -> list[dict[str, Any]]:
        assert query_text == "Bordeaux 跟其他產區的比較"
        assert region_canonical == "Bordeaux"
        return fake_hits

    monkeypatch.setattr(retrieval, "query_all_knowledge", fake_query_all_knowledge)

    result = tools.dispatch_query_terroir_knowledge(
        "Bordeaux 跟其他產區的比較", region_canonical="Bordeaux"
    )

    assert result == {"results": fake_hits}, f"應原樣包成 results，實際：{result}"
    json.dumps(result)


def test_dispatch_query_terroir_knowledge_捕捉RetrievalError並回傳error_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_query_all_knowledge(*args: Any, **kwargs: Any) -> None:
        raise retrieval.RetrievalError("知識檢索暫時無法使用", "ChromaError: collection missing")

    monkeypatch.setattr(retrieval, "query_all_knowledge", fake_query_all_knowledge)

    result = tools.dispatch_query_terroir_knowledge("風土問題")

    assert result == {
        "error": True,
        "error_type": "terroir_knowledge_retrieval",
        "user_message": "知識檢索暫時無法使用",
    }, f"應捕捉例外並回傳 error dict，不拋出，實際：{result}"


# --- dispatch_recognize_wine_label（離線，monkeypatch vision） ------------------


def test_dispatch_recognize_wine_label_呼叫recognize_label並回傳to_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_label_info = vision.LabelInfo(region="Bordeaux", winery=None, vintage=2019, grape=None)

    def fake_recognize_label(image_path: str) -> vision.LabelInfo:
        assert image_path == "data/test_labels/some_photo.jpg"
        return fake_label_info

    monkeypatch.setattr(vision, "recognize_label", fake_recognize_label)

    result = tools.dispatch_recognize_wine_label("data/test_labels/some_photo.jpg")

    assert result == fake_label_info.to_dict(), f"應回傳 LabelInfo.to_dict()，實際：{result}"
    json.dumps(result)


def test_dispatch_recognize_wine_label_捕捉VisionError並回傳error_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_recognize_label(image_path: str) -> None:
        raise vision.VisionError("這張酒標我讀不太清楚，可以手動輸入嗎？", "OpenAIError: timeout")

    monkeypatch.setattr(vision, "recognize_label", fake_recognize_label)

    result = tools.dispatch_recognize_wine_label("data/test_labels/blurry.jpg")

    assert result == {
        "error": True,
        "error_type": "label_recognition",
        "user_message": "這張酒標我讀不太清楚，可以手動輸入嗎？",
    }, f"應捕捉例外並回傳 error dict，不拋出，實際：{result}"


# --- dispatch_check_region_validity（不需 monkeypatch，只讀本機 regions.json） ----


def test_dispatch_check_region_validity_合法產區回傳valid_true() -> None:
    result = tools.dispatch_check_region_validity("Bordeaux")
    assert result == {
        "valid": True,
        "region_canonical": "Bordeaux",
        "region_zh": "波爾多",
    }, f"合法產區應回傳 valid=True 與正式名稱，實際：{result}"
    json.dumps(result)


def test_dispatch_check_region_validity_不合法產區回傳valid_false_而非拋錯() -> None:
    result = tools.dispatch_check_region_validity("Santorini")
    assert result["valid"] is False, f"不合法產區應回傳 valid=False，實際：{result}"
    assert "reason" in result, f"應附上 reason 說明，實際：{result}"
    json.dumps(result)
