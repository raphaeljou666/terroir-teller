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


# --- _extract_cited_ids ------------------------------------------------------------------


def test_extract_cited_ids只保留真實存在的id() -> None:
    body = "偏暖偏乾的年份糖度可能較高 [rule_warm_dry_01]，也可能受到 [made_up_id] 影響。"
    cited = report._extract_cited_ids(body, {"rule_warm_dry_01"})
    assert cited == ["rule_warm_dry_01"]


def test_extract_cited_ids保留首次出現順序並去重() -> None:
    body = "[b] 開頭，中間又提到 [a]，最後重複 [b] 一次。"
    cited = report._extract_cited_ids(body, {"a", "b"})
    assert cited == ["b", "a"]


def test_extract_cited_ids沒有引用時回傳空列表() -> None:
    assert report._extract_cited_ids("這段沒有任何引用標記。", {"a"}) == []


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
    body = "## 風味推測\n\n內容。\n\n## 氣候摘要\n\n內容。\n\n## 限制說明\n\n目前沒有其他限制。"
    result = report._ensure_limitation_caveats(body, "Bordeaux")
    assert "代理指標" in result


def test_ensure_limitation_caveats山區產區缺少ERA5提醒時自動補上() -> None:
    body = "## 限制說明\n\n這裡已經提到代理指標的限制。"
    result = report._ensure_limitation_caveats(body, "Barolo")
    assert "ERA5" in result
    assert "Barolo" in result


def test_ensure_limitation_caveats非山區產區不會被硬塞ERA5提醒() -> None:
    body = "## 限制說明\n\n這裡已經提到代理指標的限制。"
    result = report._ensure_limitation_caveats(body, "Bordeaux")
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


def test_generate_report組出markdown並附加決定式的資料來源段(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_body = (
        "## 風味推測\n\n偏暖偏乾的年份糖度可能較高 [rule_warm_dry_01]。\n\n"
        "## 氣候摘要\n\n2019 年偏暖。\n\n"
        "## 限制說明\n\n採收前30天降雨是代理指標，非實際採收日資料。"
    )
    monkeypatch.setattr(report, "_get_client", lambda: object())
    monkeypatch.setattr(report, "_call_report_llm", lambda client, message: fake_body)

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
    fake_body = (
        "## 風味推測\n\n這段引用了不存在的片段 [fake_id_not_real]。\n\n"
        "## 氣候摘要\n\n無資料。\n\n## 限制說明\n\n採收前30天降雨是代理指標。"
    )
    monkeypatch.setattr(report, "_get_client", lambda: object())
    monkeypatch.setattr(report, "_call_report_llm", lambda client, message: fake_body)

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
