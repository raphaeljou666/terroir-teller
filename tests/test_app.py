"""`app.py` 的測試（T-18）：離線驗證 Streamlit 主頁面流程，monkeypatch 掉 `agent.analyze`／
`vision.recognize_label`，永遠不觸發真實 OpenAI 呼叫（條款 19）。

用 `streamlit.testing.v1.AppTest` 無瀏覽器驅動整支腳本。上傳一律走 `file_uploader` 模擬
（`st.camera_input()` 已在 T-18 移除，見 `app.py` `render_upload_section()` docstring）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import agent
import app
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


# --- 逐月距平圖（T-23 改版） -----------------------------------------------------------


def _fake_comparison() -> pd.DataFrame:
    """手刻一份逐月比較資料，正負距平都有，不需要真實 API 或快取。"""
    return pd.DataFrame({
        "month": [4, 5, 6],
        "month_label": ["4月", "5月", "6月"],
        "temp_mean_vintage": [12.0, 14.5, 20.0],
        "temp_mean_baseline": [12.5, 16.0, 19.5],      # 距平：-0.5, -1.5, +0.5
        "precipitation_mm_vintage": [80.0, 45.0, 85.0],
        "precipitation_mm_baseline": [70.0, 73.0, 66.0],  # 距平：+10, -28, +19
    })


def test_距平圖畫的是差值而不是原始序列() -> None:
    fig = app._build_anomaly_chart(_fake_comparison(), "2019 年", "light")
    temp_bar, rain_bar = fig.data
    assert list(temp_bar.y) == pytest.approx([-0.5, -1.5, 0.5])
    assert list(rain_bar.y) == pytest.approx([10.0, -28.0, 19.0])


def test_距平圖用正負決定顏色而不是用序列身分() -> None:
    fig = app._build_anomaly_chart(_fake_comparison(), "2019 年", "light")
    above, below = app.ABOVE_COLOR["light"], app.BELOW_COLOR["light"]
    temp_bar, rain_bar = fig.data
    assert list(temp_bar.marker.color) == [below, below, above]
    assert list(rain_bar.marker.color) == [above, below, above]


def test_溫度與降雨分成上下兩排而不是共用雙y軸() -> None:
    """單位不同的兩個指標疊在同一張圖的兩個 y 軸上，交叉點會變成視覺巧合。"""
    fig = app._build_anomaly_chart(_fake_comparison(), "2019 年", "light")
    assert len(fig.data) == 2
    assert fig.data[0].yaxis != fig.data[1].yaxis
    assert fig.data[0].xaxis != fig.data[1].xaxis  # 上下兩排各自的子圖軸


def test_距平圖關閉縮放以免使用者縮不回原本範圍() -> None:
    """原始回饋：放大後沒有明顯方式縮回，觸控裝置上雙擊重置不直覺。"""
    fig = app._build_anomaly_chart(_fake_comparison(), "2019 年", "light")
    layout = fig.layout.to_plotly_json()
    axes = [v for k, v in layout.items() if k.startswith(("xaxis", "yaxis"))]
    assert axes, "應該要有座標軸設定"
    for axis in axes:
        assert axis.get("fixedrange") is True


@pytest.mark.parametrize("theme_type", ["light", "dark"])
def test_亮暗兩種模式都取得到配色(theme_type: str) -> None:
    fig = app._build_anomaly_chart(_fake_comparison(), "2019 年", theme_type)
    used = set(fig.data[0].marker.color) | set(fig.data[1].marker.color)
    assert used <= {app.ABOVE_COLOR[theme_type], app.BELOW_COLOR[theme_type]}
