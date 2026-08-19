"""`app.py` 的測試（T-18）：離線驗證 Streamlit 主頁面流程，monkeypatch 掉 `agent.analyze`／
`vision.recognize_label`，永遠不觸發真實 OpenAI 呼叫（條款 19）。

用 `streamlit.testing.v1.AppTest` 無瀏覽器驅動整支腳本。`AppTest` 沒有 `camera_input` 的
存取器，拍照入口在這裡測不到，上傳一律走 `file_uploader` 模擬。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from streamlit.testing.v1 import AppTest

import agent
from src import climate, vision

APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")


def _fake_ok_result(**overrides: Any) -> agent.AnalysisResult:
    """組出一個成功狀態的 `AnalysisResult`，不呼叫真的 report／climate 邏輯。"""
    gathered = agent.GatheredData(region_canonical="Bordeaux", region_zh="波爾多", vintage_year=2019)
    return agent.AnalysisResult(
        status="ok",
        markdown="## 風味推測\n測試內容\n## 資料來源\n[1] 測試來源",
        gathered=gathered,
        **overrides,
    )


def _fill_minimum_form(at: AppTest, region: str = "Bordeaux", vintage: int = 2019) -> AppTest:
    """填妥「開始分析」按鈕需要的最少欄位（產區＋年份）。"""
    at.text_input[0].set_value(region)
    at.number_input[0].set_value(vintage)
    return at.run()


# --- 初始渲染 ---------------------------------------------------------------


def test_初始渲染不拋例外() -> None:
    at = AppTest.from_file(APP_PATH)
    at.run()
    assert not at.exception


# --- 表單按鈕啟用/停用 ---------------------------------------------------------


def test_產區欄位空白時分析按鈕停用() -> None:
    at = AppTest.from_file(APP_PATH)
    at.run()
    assert at.button[0].disabled is True


def test_填妥產區與年份後按鈕啟用且按下會呼叫agent_analyze(monkeypatch: Any) -> None:
    calls: list[dict[str, Any]] = []

    def _spy(**kwargs: Any) -> agent.AnalysisResult:
        calls.append(kwargs)
        return _fake_ok_result()

    monkeypatch.setattr(agent, "analyze", _spy)

    at = AppTest.from_file(APP_PATH)
    at.run()
    _fill_minimum_form(at)
    assert at.button[0].disabled is False

    at.button[0].click().run()

    assert not at.exception
    assert len(calls) == 1
    call_kwargs = calls[0]
    assert callable(call_kwargs.pop("on_progress"))
    assert call_kwargs == {
        "region": "Bordeaux",
        "year": 2019,
        "label_info": {"winery": None, "grape": None},
    }


# --- 非成功狀態顯示白話訊息，不中斷、表單仍在 ------------------------------------------


def test_產區不在清單內時顯示白話訊息且表單仍可編輯(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        agent,
        "analyze",
        lambda **kw: agent.AnalysisResult(
            status="region_not_covered", user_message="本系統目前不涵蓋這個產區。"
        ),
    )

    at = AppTest.from_file(APP_PATH)
    at.run()
    _fill_minimum_form(at, region="某個沒收錄的產區", vintage=2020)
    at.button[0].click().run()

    assert not at.exception
    assert "本系統目前不涵蓋這個產區" in "".join(w.value for w in at.warning)
    assert len(at.text_input) == 3
    assert len(at.number_input) == 1


# --- 上傳驗證：超過 5MB ------------------------------------------------------------


def test_上傳檔案超過5mb顯示大小警告且不呼叫recognize_label(monkeypatch: Any) -> None:
    called: list[str] = []
    monkeypatch.setattr(vision, "recognize_label", lambda *a, **k: called.append("called"))

    at = AppTest.from_file(APP_PATH)
    at.run()
    oversized = b"\xff\xd8\xff" + b"0" * (vision.MAX_IMAGE_SIZE_BYTES + 1)
    at.file_uploader[0].upload("big.jpg", oversized, "image/jpeg").run()

    assert not at.exception
    assert vision.USER_MESSAGE_TOO_LARGE in "".join(w.value for w in at.warning)
    assert called == []


# --- 上傳驗證：副檔名合法但內容不是圖片（st.image 崩潰防線） ----------------------------


def test_上傳非圖片內容顯示格式警告且不崩潰不呼叫recognize_label(monkeypatch: Any) -> None:
    called: list[str] = []
    monkeypatch.setattr(vision, "recognize_label", lambda *a, **k: called.append("called"))

    at = AppTest.from_file(APP_PATH)
    at.run()
    at.file_uploader[0].upload("fake.jpg", b"not a jpeg", "image/jpeg").run()

    assert not at.exception
    assert vision.USER_MESSAGE_BAD_FORMAT in "".join(w.value for w in at.warning)
    assert called == []


# --- 上傳成功時表單自動帶入辨識結果 -----------------------------------------------------


def test_上傳有效圖片成功辨識後表單自動帶入結果(monkeypatch: Any) -> None:
    fixture_path = vision.PROJECT_ROOT / "data" / "test_labels" / "fixtures" / "valid_label.jpg"

    monkeypatch.setattr(
        vision,
        "recognize_label",
        lambda *a, **k: vision.LabelInfo(
            region="Bordeaux", winery="Château Test", vintage=2018, grape="Cabernet Sauvignon"
        ),
    )

    at = AppTest.from_file(APP_PATH)
    at.run()
    at.file_uploader[0].upload("valid_label.jpg", fixture_path.read_bytes(), "image/jpeg").run()

    assert not at.exception
    assert at.text_input[0].value == "Bordeaux"
    assert at.text_input[1].value == "Château Test"
    assert at.text_input[2].value == "Cabernet Sauvignon"
    assert at.number_input[0].value == 2018


# --- 氣候資料取不到時圖表區顯示白話說明（T-18） ------------------------------------------


def test_氣候資料取不到時圖表區顯示白話說明而非靜默消失(monkeypatch: Any) -> None:
    def _raise_climate_error(*args: Any, **kwargs: Any) -> Any:
        raise climate.ClimateDataError("氣候資料暫時無法取得，請稍後再試一次。", "測試用例外")

    monkeypatch.setattr(climate, "fetch_season_climate", _raise_climate_error)
    monkeypatch.setattr(
        agent,
        "analyze",
        lambda **kw: agent.AnalysisResult(
            status="ok",
            markdown="## 風味推測\n測試內容\n## 資料來源\n[1] 測試來源",
            gathered=agent.GatheredData(
                region_canonical="測試專用不存在產區", region_zh="測試", vintage_year=2019
            ),
        ),
    )

    at = AppTest.from_file(APP_PATH)
    at.run()
    _fill_minimum_form(at)
    at.button[0].click().run()

    assert not at.exception
    assert "暫時無法取得" in "".join(i.value for i in at.info)


# --- 距平卡片：pct_anomaly 為 None 時不拋例外（T-18） -------------------------------------


def test_距平百分比為None時距平卡片不拋例外且不顯示delta(monkeypatch: Any) -> None:
    anomaly = {
        "gdd": {"vintage_value": 1500.0, "pct_anomaly": None},
        "season_precipitation": {"vintage_value": 600.0, "pct_anomaly": None},
    }
    monkeypatch.setattr(
        agent,
        "analyze",
        lambda **kw: agent.AnalysisResult(
            status="ok",
            markdown="## 風味推測\n測試內容\n## 資料來源\n[1] 測試來源",
            gathered=agent.GatheredData(
                region_canonical="Bordeaux", region_zh="波爾多", vintage_year=2019, anomaly=anomaly
            ),
        ),
    )

    at = AppTest.from_file(APP_PATH)
    at.run()
    _fill_minimum_form(at)
    at.button[0].click().run()

    assert not at.exception
    assert len(at.metric) == 2
    assert all(m.delta == "" for m in at.metric)


# --- 成本控管回歸測試（條款 19，最重要） -----------------------------------------------


def test_已有結果後編輯欄位觸發rerun不會重複呼叫agent_analyze(monkeypatch: Any) -> None:
    calls: list[dict[str, Any]] = []

    def _spy(**kwargs: Any) -> agent.AnalysisResult:
        calls.append(kwargs)
        return _fake_ok_result()

    monkeypatch.setattr(agent, "analyze", _spy)

    at = AppTest.from_file(APP_PATH)
    at.run()
    _fill_minimum_form(at)
    at.button[0].click().run()
    assert len(calls) == 1

    at.text_input[1].set_value("Château Test").run()  # 編輯選填欄位（酒莊）
    assert len(calls) == 1, "編輯表單欄位觸發的 rerun 不應該再打一次 agent.analyze()"

    at.number_input[0].set_value(2020).run()  # 再編輯一個欄位
    assert len(calls) == 1, "編輯表單欄位觸發的 rerun 不應該再打一次 agent.analyze()"
