"""TerroirTeller Streamlit 主頁面（T-14）：上傳酒標 → 確認辨識結果 → 分析 → 報告呈現。

單頁式流程，串起 `agent.analyze()` 與 `climate.build_monthly_comparison()`。分析邏輯只
寫在按鈕點擊後面、結果存進 `st.session_state`，避免 Streamlit 每次 rerun 都重複呼叫
付費 API（`06_TechSetup.md` §11 踩雷點）。

手動輸入 fallback（T-17）不是獨立模式，而是同一份表單：辨識失敗、辨識不出產區、或使用者
根本沒上傳圖片，欄位就是空的，使用者直接打字即可，不會走進死路。
"""

from __future__ import annotations

import logging
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import agent
from src import climate, vision
from src.report import MOUNTAIN_RAINFALL_BIAS_REGIONS

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

MIN_VINTAGE_YEAR = 1940

USER_MESSAGE_BAD_FORMAT = "這張圖片的格式看起來不是 JPG 或 PNG，請重新拍照或選擇其他檔案。"
USER_MESSAGE_TOO_LARGE = "這張圖片檔案太大了（超過 5MB），請重新拍照或壓縮後再試。"


def _init_session_state() -> None:
    """初始化這次 session 需要的欄位，只在第一次執行時設定預設值。"""
    st.session_state.setdefault("processed_file_id", None)
    st.session_state.setdefault(
        "label_form", {"region": "", "winery": "", "vintage": None, "grape": ""}
    )
    st.session_state.setdefault("result", None)
    st.session_state.setdefault("upload_warning", None)


def _temp_image_path(uploaded_file: Any) -> Path:
    """把上傳的圖片寫進系統暫存目錄，回傳路徑給 `vision.recognize_label()` 使用。"""
    suffix = Path(uploaded_file.name).suffix or ".jpg"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
        handle.write(uploaded_file.getvalue())
        return Path(handle.name)


def _run_vision_recognition(image_path: str) -> dict[str, Any] | None:
    """呼叫酒標辨識，失敗時回傳 `None` 並把白話訊息存進 session_state，不中斷流程（T-17）。"""
    try:
        label_info = vision.recognize_label(image_path)
    except vision.VisionError as exc:
        logger.error("酒標辨識失敗，技術細節：%s", exc.technical_detail)
        st.session_state.upload_warning = exc.user_message
        return None
    return label_info.to_dict()


def render_upload_section() -> None:
    """兩個上傳入口（拍照／選擇檔案，條款 26），偵測到新圖片時驗證、存檔、辨識、填表單。

    驗證（副檔名／大小／內容能不能被解碼）一定要在 `st.image()` 顯示縮圖之前做完——
    `st.image()` 遇到無法解碼的內容會直接拋例外，順序顛倒會讓使用者看到 stack trace
    而不是白話錯誤訊息（條款 18、US-4.1「不會白畫面」）。
    """
    st.subheader("上傳酒標")
    camera_file = st.camera_input("拍照上傳酒標")
    uploaded_file = st.file_uploader("或選擇圖片檔案", type=["jpg", "jpeg", "png"])
    image_file = camera_file or uploaded_file

    if image_file is None:
        return

    is_new_file = image_file.file_id != st.session_state.processed_file_id
    if is_new_file:
        st.session_state.processed_file_id = image_file.file_id
        st.session_state.upload_warning = None

        if Path(image_file.name).suffix.lower() not in vision.ALLOWED_EXTENSIONS:
            st.session_state.upload_warning = USER_MESSAGE_BAD_FORMAT
        elif len(image_file.getvalue()) > vision.MAX_IMAGE_SIZE_BYTES:
            st.session_state.upload_warning = USER_MESSAGE_TOO_LARGE

    if st.session_state.upload_warning:
        return

    try:
        st.image(image_file, caption="已上傳的酒標", width=240)
    except Exception as exc:  # noqa: BLE001 — st.image 對無法解碼的內容拋的例外型別不固定
        logger.error("縮圖顯示失敗，技術細節：%r", exc)
        st.session_state.upload_warning = USER_MESSAGE_BAD_FORMAT
        return

    if not is_new_file:
        return

    with st.spinner("正在辨識酒標……"):
        image_path = _temp_image_path(image_file)
        label_info = _run_vision_recognition(str(image_path))

    if label_info:
        st.session_state.label_form = {
            "region": label_info.get("region") or "",
            "winery": label_info.get("winery") or "",
            "vintage": label_info.get("vintage"),
            "grape": label_info.get("grape") or "",
        }


def render_label_form() -> tuple[bool, dict[str, Any]]:
    """渲染四個可編輯欄位＋送出按鈕，回傳 `(是否按下, 目前欄位值)`。

    刻意不在這裡呼叫 `agent.analyze()`（渲染與副作用分開，方便測試與閱讀）。
    """
    if st.session_state.upload_warning:
        st.warning(st.session_state.upload_warning)

    st.subheader("確認辨識結果")
    with st.expander("本系統目前涵蓋的 20 個產區"):
        st.caption(
            "、".join(f"{r['region_canonical']}（{r['region_zh']}）" for r in climate.load_regions())
        )

    form = st.session_state.label_form
    region = st.text_input("產區", value=form.get("region", ""))
    winery = st.text_input("酒莊（選填）", value=form.get("winery", ""))
    grape = st.text_input("品種（選填）", value=form.get("grape", ""))
    vintage = st.number_input(
        "年份", min_value=MIN_VINTAGE_YEAR, max_value=date.today().year,
        value=form.get("vintage"), step=1,
    )

    st.session_state.label_form = {"region": region, "winery": winery, "vintage": vintage, "grape": grape}

    button_label = "重新分析" if st.session_state.result else "開始分析"
    pressed = st.button(button_label, disabled=not region.strip() or vintage is None)
    return pressed, st.session_state.label_form


def run_analysis(values: dict[str, Any]) -> None:
    """按鈕按下後呼叫，包在 spinner 裡執行 `agent.analyze()`，結果存進 `session_state`。"""
    region = values["region"].strip()
    year = int(values["vintage"])
    logger.info("呼叫 agent.analyze()：region=%r, year=%r", region, year)
    with st.spinner("正在查氣候資料與知識庫……"):
        st.session_state.result = agent.analyze(
            region=region,
            year=year,
            label_info={
                "winery": (values.get("winery") or "").strip() or None,
                "grape": (values.get("grape") or "").strip() or None,
            },
        )


def render_report(result: agent.AnalysisResult) -> None:
    """依 `result.status` 分支渲染：成功顯示報告＋圖表，其餘顯示白話說明（US-4.1）。"""
    if result.status != "ok" or result.markdown is None:
        st.warning(result.user_message)
        return

    body, _, sources = result.markdown.partition("## 資料來源")
    st.markdown(body)
    render_charts(result.gathered)
    with st.expander("資料來源", expanded=True):
        st.markdown(sources or "本次報告未能引用任何具體知識庫片段或氣候資料。")


@st.cache_data(show_spinner=False)
def _load_monthly_comparison(region_canonical: str, vintage_year: int) -> pd.DataFrame | None:
    """氣候資料額外快取一層，避免 Streamlit rerun 時重複讀磁碟快取、重建 dataclass。"""
    try:
        season = climate.fetch_season_climate(region_canonical, vintage_year)
        baseline = climate.get_baseline(region_canonical)
    except climate.ClimateDataError as exc:
        logger.error("氣候圖表資料取得失敗，技術細節：%s", exc.technical_detail)
        return None
    return climate.build_monthly_comparison(season, baseline)


def render_charts(gathered: agent.GatheredData) -> None:
    """畫每月均溫折線圖與降雨柱狀圖（T-15、US-4.2），山區產區加 ERA5 高估提醒。"""
    if not gathered.region_canonical or not gathered.vintage_year:
        return

    comparison = _load_monthly_comparison(gathered.region_canonical, gathered.vintage_year)
    if comparison is None:
        return

    st.subheader("氣候距平圖表")
    year_label = f"{gathered.vintage_year} 年"

    temp_fig = go.Figure()
    temp_fig.add_trace(go.Scatter(x=comparison["month_label"], y=comparison["temp_mean_vintage"], name=year_label))
    temp_fig.add_trace(go.Scatter(x=comparison["month_label"], y=comparison["temp_mean_baseline"], name="30 年平均"))
    temp_fig.update_layout(title="每月均溫（該年 vs 30 年平均）", yaxis_title="°C")
    st.plotly_chart(temp_fig, width="stretch")

    rain_fig = go.Figure()
    rain_fig.add_trace(go.Bar(x=comparison["month_label"], y=comparison["precipitation_mm_vintage"], name=year_label))
    rain_fig.add_trace(go.Bar(x=comparison["month_label"], y=comparison["precipitation_mm_baseline"], name="30 年平均"))
    rain_fig.update_layout(title="每月降雨量（該年 vs 30 年平均）", yaxis_title="mm", barmode="group")
    st.plotly_chart(rain_fig, width="stretch")

    if gathered.region_canonical in MOUNTAIN_RAINFALL_BIAS_REGIONS:
        st.caption(
            f"{gathered.region_canonical} 地形起伏較大，ERA5 氣候資料在山區容易高估降雨量"
            "（約為實測值的兩倍），這裡的降雨數字可以保留一定的懷疑空間。"
        )


def main() -> None:
    """串起上傳、表單、分析、報告呈現各步驟。"""
    st.set_page_config(page_title="TerroirTeller", page_icon="🍷")
    _init_session_state()

    st.title("🍷 TerroirTeller")
    st.caption("拍一張酒標，看看那一年的氣候可能帶來什麼風味傾向。")

    render_upload_section()
    pressed, values = render_label_form()

    if pressed:
        run_analysis(values)

    if st.session_state.result is not None:
        render_report(st.session_state.result)


if __name__ == "__main__":
    main()
