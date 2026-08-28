"""`src.retrieval` 的測試。

Ingestion／query 測試對外部 OpenAI embedding API 有真實依賴：故意選擇打真實 API 而不是
mock（本專案沒有 CI 管線，且檢索品質是語意相似度問題，mock 只能測到 mock 本身），用
climate_rules 10 個檔案的小樣本控制成本（條款 19）。沒有 `OPENAI_API_KEY` 時真實 API
那組測試會 skip，而不是失敗；離線測試（frontmatter 解析、metadata 扁平化、檔案發現）
永遠會跑，不需要任何外部依賴。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from src import retrieval

NEEDS_OPENAI_KEY = pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"), reason="需要 OPENAI_API_KEY 才能測試真實檢索品質"
)


# --- Frontmatter／body 解析（離線） -------------------------------------------


def test_解析出的_frontmatter_含必要欄位() -> None:
    frontmatter, body = retrieval.parse_markdown_with_frontmatter(
        retrieval.CLIMATE_RULES_DIR / "warm_dry.md"
    )
    assert frontmatter["id"] == "rule_warm_dry_01"
    assert frontmatter["schema_version"] == "0.3"
    assert body  # 內文不為空


def test_缺少分隔線的檔案會拋出可辨識例外(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad.md"
    bad_file.write_text("沒有 frontmatter 的檔案", encoding="utf-8")
    with pytest.raises(retrieval.RetrievalError):
        retrieval.parse_markdown_with_frontmatter(bad_file)


def test_YAML_格式錯誤時拋出可辨識例外(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad_yaml.md"
    bad_file.write_text("---\nkey: [unclosed\n---\n內文", encoding="utf-8")
    with pytest.raises(retrieval.RetrievalError):
        retrieval.parse_markdown_with_frontmatter(bad_file)


# --- Metadata 扁平化（離線） ---------------------------------------------------


def test_巢狀_dict_與_list_被扁平化為_Chroma_接受的型別() -> None:
    frontmatter, _ = retrieval.parse_markdown_with_frontmatter(
        retrieval.CLIMATE_RULES_DIR / "warm_dry.md"
    )
    metadata = retrieval.build_chroma_metadata(
        frontmatter, "climate_rule", retrieval.CLIMATE_RULES_DIR / "warm_dry.md"
    )
    for value in metadata.values():
        assert isinstance(value, (str, int, float, bool))
    assert metadata["layer"] == "climate_rule"
    assert "偏暖" in metadata["tags"]  # tags 是逗號合併字串
    assert "id" not in metadata  # id 已經拿去當 Chroma 文件 id，不重複存


def test_region_layer_檔案會補上_layer_region_與_topic() -> None:
    path = retrieval.REGIONS_DIR / "bordeaux" / "01_climate.md"
    frontmatter, _ = retrieval.parse_markdown_with_frontmatter(path)
    metadata = retrieval.build_chroma_metadata(frontmatter, "region", path)
    assert metadata["layer"] == "region"
    assert metadata["topic"] == "climate"
    assert metadata["region_canonical"] == "Bordeaux"


def test_climate_rule的condition被攤平成可過濾的純量欄位() -> None:
    path = retrieval.CLIMATE_RULES_DIR / "warm_dry.md"
    frontmatter, _ = retrieval.parse_markdown_with_frontmatter(path)
    metadata = retrieval.build_chroma_metadata(frontmatter, "climate_rule", path)
    assert metadata["condition_temperature"] == "warmer"
    assert metadata["condition_precipitation"] == "drier"
    assert metadata["condition_magnitude"] == "moderate"
    assert metadata["condition_duration"] == "growing_season"
    # 原本的 JSON 字串欄位保留不動，既有的顯示／除錯用途不受影響。
    assert "temperature" in metadata["condition"]


def test_十則氣候規則的condition_temperature都在封閉詞彙內且兩極都非空() -> None:
    """資料完整性測試：防止日後打錯字讓某則規則永遠檢索不到。

    方向過濾是硬 `where` 條件，某則規則的 `condition.temperature` 一旦打錯（例如寫成
    `"cool"` 而非 `"cooler"`），它就會從所有查詢結果中無聲消失，不會報錯。這條測試是
    那種失誤的唯一防線，而且完全免費。
    """
    directions: dict[str, list[str]] = {}
    for path, layer in retrieval.discover_knowledge_files(sample_only=True):
        frontmatter, _ = retrieval.parse_markdown_with_frontmatter(path)
        metadata = retrieval.build_chroma_metadata(frontmatter, layer, path)
        value = metadata["condition_temperature"]
        assert value in {"warmer", "cooler", "n/a"}, f"{path.name} 的溫度詞彙不在封閉集合內"
        directions.setdefault(value, []).append(frontmatter["id"])

    # 兩極都要有規則，否則方向過濾會讓其中一個方向查不到任何東西。
    assert directions.get("warmer"), "沒有任何偏暖規則"
    assert directions.get("cooler"), "沒有任何偏涼規則"


def test_none_值的欄位不會寫入_metadata() -> None:
    path = retrieval.REGIONS_DIR / "bordeaux" / "01_climate.md"
    frontmatter, _ = retrieval.parse_markdown_with_frontmatter(path)
    assert frontmatter.get("sub_region") is None
    metadata = retrieval.build_chroma_metadata(frontmatter, "region", path)
    assert "sub_region" not in metadata


# --- 檔案發現（離線） ----------------------------------------------------------


def test_sample_only_只回傳_climate_rules_十個檔案() -> None:
    files = retrieval.discover_knowledge_files(sample_only=True)
    assert len(files) == 10
    assert all(layer == "climate_rule" for _, layer in files)


def test_全量模式回傳九十八個檔案() -> None:
    files = retrieval.discover_knowledge_files(sample_only=False)
    assert len(files) == 98
    layers = [layer for _, layer in files]
    assert layers.count("climate_rule") == 10
    assert layers.count("region") == 88


def test_全量文件都能成功載入且_id_不重複() -> None:
    documents = retrieval.load_documents(sample_only=False)
    assert len(documents) == 98
    ids = [doc["id"] for doc in documents]
    assert len(ids) == len(set(ids)), "frontmatter id 應該互不重複"


# --- 方向過濾的 where 結構（離線，monkeypatch _query） --------------------------


def _capture_where(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """攔截 `_query()` 收到的 where 參數。

    `_query` 是通往 Chroma 的唯一入口，攔在這裡就能離線驗證整個 where 組裝邏輯，
    不需要真的建索引或打 embedding API。
    """
    captured: dict[str, Any] = {}

    def fake_query(query_text: str, n_results: int, where: dict[str, Any] | None) -> list:
        captured["where"] = where
        return []

    monkeypatch.setattr(retrieval, "_query", fake_query)
    return captured


def test_偏涼年份的where限制規則在cooler與na(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_where(monkeypatch)
    retrieval.query_climate_knowledge("生長季偏涼", temperature_direction="cooler")
    rule_branch = captured["where"]["$or"][0]
    assert rule_branch["$and"][0] == {"layer": "climate_rule"}
    assert rule_branch["$and"][1] == {"condition_temperature": {"$in": ["cooler", "n/a"]}}


def test_偏暖年份的where限制規則在warmer與na(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_where(monkeypatch)
    retrieval.query_climate_knowledge("生長季偏暖", temperature_direction="warmer")
    rule_branch = captured["where"]["$or"][0]
    assert rule_branch["$and"][1] == {"condition_temperature": {"$in": ["warmer", "n/a"]}}


def test_方向不明時where與加入方向過濾前完全相同(monkeypatch: pytest.MonkeyPatch) -> None:
    """方向為 None 要退回原本行為，不是硬猜一個方向（呼應條款 15）。"""
    captured = _capture_where(monkeypatch)
    retrieval.query_climate_knowledge("生長季溫度接近平均", temperature_direction=None)
    assert captured["where"]["$or"][0] == {"layer": "climate_rule"}


def test_方向過濾不會動到regions那一支(monkeypatch: pytest.MonkeyPatch) -> None:
    """條款 36 的兩層過濾不能被方向過濾影響——產區氣候片段沒有 condition 欄位。"""
    captured = _capture_where(monkeypatch)
    retrieval.query_climate_knowledge(
        "生長季偏涼", region_canonical="Bordeaux", temperature_direction="cooler"
    )
    region_branch = captured["where"]["$or"][1]
    assert {"layer": "region"} in region_branch["$and"]
    assert {"topic": "climate"} in region_branch["$and"]
    assert {"region_canonical": "Bordeaux"} in region_branch["$and"]
    assert not any("condition_temperature" in str(cond) for cond in region_branch["$and"])


# --- 真實檢索品質（需要 OPENAI_API_KEY，會打真實 API） -----------------------


@NEEDS_OPENAI_KEY
def test_偏暖偏乾查詢會命中對應的_climate_rules_片段(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(retrieval, "CHROMA_DIR", tmp_path)
    retrieval.ingest_knowledge_base(sample_only=True, reset=True)
    hits = retrieval.query_climate_knowledge("偏暖偏乾的年份", n_results=3)
    hit_ids = {hit["id"] for hit in hits}
    assert hit_ids & {"rule_warm_dry_01", "rule_drought_09"}, hit_ids


@NEEDS_OPENAI_KEY
def test_氣候檢索路徑不會回傳_terroir_或_comparison_片段(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(retrieval, "CHROMA_DIR", tmp_path)
    retrieval.ingest_knowledge_base(sample_only=False, reset=True)
    hits = retrieval.query_climate_knowledge("Bordeaux 的風土跟其他產區有什麼不同", n_results=10)
    for hit in hits:
        is_climate_rule = hit["metadata"]["layer"] == "climate_rule"
        is_region_climate = hit["metadata"].get("topic") == "climate"
        assert is_climate_rule or is_region_climate


@NEEDS_OPENAI_KEY
def test_all_knowledge_檢索路徑可以命中_comparison_片段(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(retrieval, "CHROMA_DIR", tmp_path)
    retrieval.ingest_knowledge_base(sample_only=False, reset=True)
    hits = retrieval.query_all_knowledge(
        "Bordeaux 跟其他產區的比較", n_results=5, region_canonical="Bordeaux"
    )
    assert any(hit["metadata"].get("topic") == "comparison" for hit in hits)
