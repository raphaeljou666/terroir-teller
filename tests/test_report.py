"""`src.report` 的單元測試。

全部離線執行，不打 OpenAI API：`generate_report()` 對 LLM 呼叫的部分用 monkeypatch 替換。
測試重點放在條款 15／17 最攸關的邏輯——資料來源段落是程式碼決定式組裝、不是 LLM 生成，
以及模型編造不存在的 `[chunk_id]` 時會被過濾掉、不會流到使用者畫面上。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from src import report


def _make_hit(chunk_id: str, confidence: str = "high", tags: str = "偏暖, 偏乾") -> dict[str, Any]:
    metadata = {
        "confidence": confidence,
        "tags": tags,
        "topic": "climate",
        "sources": json.dumps(
            [{"title": "The Oxford Companion to Wine", "author": "Robinson, J.", "year": 2023}],
            ensure_ascii=False,
        ),
    }
    return {"id": chunk_id, "document": "這是知識片段內文。" * 5, "metadata": metadata, "distance": 0.1}


# --- _collect_cited_ids ------------------------------------------------------------------


def test_collect_cited_ids只保留真實存在的id() -> None:
    paragraphs = [
        {"text": "偏暖偏乾的年份糖度可能較高。", "cited_ids": ["rule_warm_dry_01", "made_up_id"]},
    ]
    cited = report._collect_cited_ids(paragraphs, {"rule_warm_dry_01"})
    assert cited == ["rule_warm_dry_01"]


def test_collect_cited_ids保留首次出現順序並去重() -> None:
    paragraphs = [
        {"text": "第一段。", "cited_ids": ["b"]},
        {"text": "第二段。", "cited_ids": ["a", "b"]},
    ]
    cited = report._collect_cited_ids(paragraphs, {"a", "b"})
    assert cited == ["b", "a"]


def test_collect_cited_ids沒有引用時回傳空列表() -> None:
    paragraphs = [{"text": "這段沒有任何引用。", "cited_ids": []}]
    assert report._collect_cited_ids(paragraphs, {"a"}) == []


# --- _render_flavor_section ---------------------------------------------------------------


def test_render_flavor_section把cited_ids渲染成括號標記() -> None:
    paragraphs = [{"text": "偏暖偏乾的年份糖度可能較高。", "cited_ids": ["rule_warm_dry_01"]}]
    section = report._render_flavor_section(paragraphs)
    assert section == "偏暖偏乾的年份糖度可能較高。 [rule_warm_dry_01]"


def test_render_flavor_section沒有引用時不留多餘空白() -> None:
    paragraphs = [{"text": "這段沒有引用。", "cited_ids": []}]
    assert report._render_flavor_section(paragraphs) == "這段沒有引用。"


# --- _build_sources_section ---------------------------------------------------------------


def test_build_sources_section依cited_ids組出confidence與出處() -> None:
    hit_lookup = {"rule_warm_dry_01": _make_hit("rule_warm_dry_01")["metadata"]}
    section = report._build_sources_section(["rule_warm_dry_01"], hit_lookup, anomaly=None)
    assert "rule_warm_dry_01" in section
    assert "confidence: high" in section
    assert "Robinson, J.（2023）" in section


def test_build_sources_section附加氣候資料出處行() -> None:
    anomaly = {"baseline_start_year": 1991, "baseline_end_year": 2020}
    section = report._build_sources_section([], {}, anomaly)
    assert "Open-Meteo" in section
    assert "1991–2020" in section


def test_build_sources_section完全沒有引用也沒有氣候資料時給出誠實說明() -> None:
    section = report._build_sources_section([], {}, None)
    assert "未能引用" in section


# --- _ensure_limitation_caveats -----------------------------------------------------------


def test_ensure_limitation_caveats缺少代理值提醒時自動補上() -> None:
    limitations = "目前沒有其他限制。"
    result = report._ensure_limitation_caveats(limitations, "Bordeaux")
    assert "代理指標" in result


def test_ensure_limitation_caveats山區產區缺少ERA5提醒時自動補上() -> None:
    limitations = "這裡已經提到代理指標的限制。"
    result = report._ensure_limitation_caveats(limitations, "Barolo")
    assert "ERA5" in result
    assert "Barolo" in result


def test_ensure_limitation_caveats非山區產區不會被硬塞ERA5提醒() -> None:
    limitations = "這裡已經提到代理指標的限制。"
    result = report._ensure_limitation_caveats(limitations, "Bordeaux")
    assert "ERA5" not in result


# --- _build_climate_context ----------------------------------------------------------------


def test_build_climate_context沒有資料時誠實說明不編造數字() -> None:
    context = report._build_climate_context(None)
    assert "無法取得" in context
    assert "編造" in context


def test_build_climate_context有資料時帶出具體距平數字() -> None:
    anomaly = {
        "summary": "Bordeaux（波爾多）2019 GDD 比 30 年平均高 12%。",
        "baseline_start_year": 1991,
        "baseline_end_year": 2020,
        "gdd": {"pct_anomaly": 12.3, "z_score": 1.5},
        "season_precipitation": {"pct_anomaly": -20.0, "z_score": -0.8},
        "pre_harvest_precipitation": {"pct_anomaly": None, "z_score": None},
        "harvest_proxy_window_start": "2019-10-02",
        "harvest_proxy_window_end": "2019-10-31",
    }
    context = report._build_climate_context(anomaly)
    assert "+12.3%" in context
    assert "無法計算" in context
    assert "2019-10-02" in context


# --- generate_report（LLM 呼叫用 monkeypatch 隔離） -----------------------------------------


def _fake_structured_body(
    flavor_inference: list[dict[str, Any]], climate_summary: str = "2019 年偏暖。",
    limitations: str = "採收前30天降雨是代理指標，非實際採收日資料。",
) -> dict[str, Any]:
    return {
        "flavor_inference": flavor_inference,
        "climate_summary": climate_summary,
        "limitations": limitations,
    }


def test_generate_report組出markdown並附加決定式的資料來源段(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_body = _fake_structured_body(
        [{"text": "偏暖偏乾的年份糖度可能較高。", "cited_ids": ["rule_warm_dry_01"]}]
    )
    monkeypatch.setattr(report, "_get_client", lambda: object())
    monkeypatch.setattr(report, "_generate_structured_body", lambda client, message, known_ids: fake_body)

    markdown = report.generate_report(
        region_canonical="Bordeaux",
        region_zh="波爾多",
        vintage_year=2019,
        anomaly=None,
        knowledge_hits=[_make_hit("rule_warm_dry_01")],
    )

    assert "## 風味推測" in markdown
    assert "## 資料來源" in markdown
    assert "rule_warm_dry_01" in markdown.split("## 資料來源")[1]


def test_generate_report過濾模型編造的引用不讓假id出現在資料來源(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_body = _fake_structured_body(
        [{"text": "這段引用了不存在的片段。", "cited_ids": ["fake_id_not_real"]}]
    )
    monkeypatch.setattr(report, "_get_client", lambda: object())
    monkeypatch.setattr(report, "_generate_structured_body", lambda client, message, known_ids: fake_body)

    markdown = report.generate_report(
        region_canonical="Bordeaux", region_zh="波爾多", vintage_year=2019,
        anomaly=None, knowledge_hits=[_make_hit("rule_warm_dry_01")],
    )

    sources_section = markdown.split("## 資料來源")[1]
    assert "fake_id_not_real" not in sources_section


def test_generate_report沒有api_key時拋出ReportGenerationError(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(report, "load_dotenv", lambda: None)

    with pytest.raises(report.ReportGenerationError):
        report.generate_report(
            region_canonical="Bordeaux", region_zh="波爾多", vintage_year=2019,
            anomaly=None, knowledge_hits=[],
        )


# --- _generate_structured_body（重試邏輯） ----------------------------------------------


def test_generate_structured_body完全沒有引用且有知識片段時重試一次(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty_body = _fake_structured_body([{"text": "沒有引用的段落。", "cited_ids": []}])
    retried_body = _fake_structured_body(
        [{"text": "重試後有引用的段落。", "cited_ids": ["rule_warm_dry_01"]}]
    )
    calls: list[str] = []

    def fake_call_once(client: Any, message: str, schema: dict[str, Any]) -> dict[str, Any]:
        calls.append(message)
        return retried_body if len(calls) > 1 else empty_body

    monkeypatch.setattr(report, "_call_report_llm_once", fake_call_once)

    result = report._generate_structured_body(object(), "使用者訊息", {"rule_warm_dry_01"})

    assert len(calls) == 2
    assert result == retried_body


def test_generate_structured_body重試後仍空就誠實接受不再重試(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty_body = _fake_structured_body([{"text": "沒有引用的段落。", "cited_ids": []}])
    calls: list[str] = []

    def fake_call_once(client: Any, message: str, schema: dict[str, Any]) -> dict[str, Any]:
        calls.append(message)
        return empty_body

    monkeypatch.setattr(report, "_call_report_llm_once", fake_call_once)

    result = report._generate_structured_body(object(), "使用者訊息", {"rule_warm_dry_01"})

    assert len(calls) == 2
    assert result == empty_body


def test_generate_structured_body沒有知識片段時空引用不觸發重試(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty_body = _fake_structured_body([{"text": "沒有引用的段落。", "cited_ids": []}])
    calls: list[str] = []

    def fake_call_once(client: Any, message: str, schema: dict[str, Any]) -> dict[str, Any]:
        calls.append(message)
        return empty_body

    monkeypatch.setattr(report, "_call_report_llm_once", fake_call_once)

    result = report._generate_structured_body(object(), "使用者訊息", set())

    assert len(calls) == 1
    assert result == empty_body


# --- _call_report_llm_once（API 呼叫與回應解析） -----------------------------------------


def test_call_report_llm_once回應無法解析為json時拋出ReportGenerationError(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeMessage:
        content = "不是合法的 JSON"

    class _FakeChoice:
        message = _FakeMessage()

    class _FakeResponse:
        choices = [_FakeChoice()]

    class _FakeCompletions:
        def create(self, **kwargs: Any) -> _FakeResponse:
            return _FakeResponse()

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    schema = report._build_report_json_schema({"a"})
    with pytest.raises(report.ReportGenerationError):
        report._call_report_llm_once(_FakeClient(), "使用者訊息", schema)


# --- _build_report_json_schema -----------------------------------------------------------


def test_build_report_json_schema有known_ids時cited_ids用enum限定() -> None:
    schema = report._build_report_json_schema({"a", "b"})
    item_schema = schema["schema"]["properties"]["flavor_inference"]["items"]["properties"]["cited_ids"]["items"]
    assert item_schema["enum"] == ["a", "b"]


def test_build_report_json_schema沒有known_ids時不帶enum() -> None:
    schema = report._build_report_json_schema(set())
    item_schema = schema["schema"]["properties"]["flavor_inference"]["items"]["properties"]["cited_ids"]["items"]
    assert "enum" not in item_schema
