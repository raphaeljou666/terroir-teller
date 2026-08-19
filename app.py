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

# 均溫／降雨兩張圖表固定的角色配色：橘色＝該年（比較主體）、藍色＝30 年平均（參照基準），
# 兩張圖用同一組，不因資料而互換。依 st.context.theme.type 挑對應色階，跟 .streamlit/
# config.toml 的主題保持同步。色碼已用 dataviz skill 的 validate_palette.js 驗證過
# CVD 安全性與對比（亮／暗模式皆 ALL CHECKS PASS，見 T-18 實作備註）。
VINTAGE_COLOR = {"light": "#eb6834", "dark": "#d95926"}
BASELINE_COLOR = {"light": "#2a78d6", "dark": "#3987e5"}


def _init_session_state() -> None:
    """初始化這次 session 需要的欄位，只在第一次執行時設定預設值。"""
    st.session_state.setdefault("processed_file_id", None)
    st.session_state.setdefault(
        "label_form", {"region": "", "winery": "", "vintage": None, "grape": ""}
    )
    st.session_state.setdefault("result", None)
    st.session_state.setdefault("upload_warning", None)
    st.session_state.setdefault("temp_image_path", None)


def _temp_image_path(uploaded_file: Any) -> Path:
    """把上傳的圖片寫進系統暫存目錄，回傳路徑給 `vision.recognize_label()` 使用。

    上一張圖片對應的暫存檔在這裡順便清掉（路徑存在 `st.session_state.temp_image_path`）
    ——絕不會刪到正在使用的檔案，因為刪除的一定是「上一輪」寫入的舊檔，這次要用的新檔
    還沒建立。單機 demo 用途，session 結束時最後一張圖片的暫存檔不會被清（作業系統的
    暫存目錄本身會定期清理），這是刻意接受的低風險殘留，不是遺漏。
    """
    previous_path = st.session_state.get("temp_image_path")
    if previous_path is not None:
        try:
            Path(previous_path).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("清理舊暫存圖片失敗（不影響本次流程）：%r", exc)

    suffix = Path(uploaded_file.name).suffix or ".jpg"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
        handle.write(uploaded_file.getvalue())
        new_path = Path(handle.name)

    st.session_state.temp_image_path = str(new_path)
    return new_path


def _run_vision_recognition(image_path: str) -> dict[str, Any] | None:
    """呼叫酒標辨識，失敗時回傳 `None` 並把白話訊息存進 session_state，不中斷流程（T-17）。"""
    try:
        label_info = vision.recognize_label(image_path)
    except vision.VisionError as exc:
        logger.error("酒標辨識失敗，技術細節：%s", exc.technical_detail)
        st.session_state.upload_warning = exc.user_message
        return None
    return label_info.to_dict()


def _validate_uploaded_file(image_file: Any) -> str | None:
    """檢查副檔名與檔案大小，回傳對應的白話警告訊息；沒問題時回傳 `None`。"""
    if Path(image_file.name).suffix.lower() not in vision.ALLOWED_EXTENSIONS:
        return vision.USER_MESSAGE_BAD_FORMAT
    if len(image_file.getvalue()) > vision.MAX_IMAGE_SIZE_BYTES:
        return vision.USER_MESSAGE_TOO_LARGE
    return None


def render_upload_section() -> None:
    """上傳入口（條款 26 已覆寫，見下方），偵測到新圖片時驗證、存檔、辨識、填表單。

    移除了原本的 `st.camera_input()`：它需要瀏覽器的安全情境（HTTPS 或 localhost）才能
    要到相機權限，實測手機連區網 IP（`http://<LAN IP>:8501`，條款 31 的 Phase 2 測試
    情境）會卡在權限請求畫面、永遠拿不到授權。改成只留 `st.file_uploader()`——手機瀏覽器
    的原生檔案選擇器本身就有「拍照」選項，拍照能力沒有消失。等 Phase 3 部署到 Streamlit
    Community Cloud（原生 HTTPS）後可重新評估要不要加回來。

    驗證（副檔名／大小／內容能不能被解碼）一定要在 `st.image()` 顯示縮圖之前做完——
    `st.image()` 遇到無法解碼的內容會直接拋例外，順序顛倒會讓使用者看到 stack trace
    而不是白話錯誤訊息（條款 18、US-4.1「不會白畫面」）。標題文字由外層的 `st.expander`
    標籤負責，這裡不重複加一個 subheader。
    """
    image_file = st.file_uploader("上傳或拍攝酒標照片", type=["jpg", "jpeg", "png"])

    if image_file is None:
        return

    is_new_file = image_file.file_id != st.session_state.processed_file_id
    if is_new_file:
        st.session_state.processed_file_id = image_file.file_id
        st.session_state.upload_warning = _validate_uploaded_file(image_file)

    if st.session_state.upload_warning:
        return

    try:
        thumbnail_bytes = vision.make_display_thumbnail_bytes(
            image_file.getvalue(), image_file.name
        )
        st.image(thumbnail_bytes, caption="已上傳的酒標", width=240)
    except Exception as exc:  # noqa: BLE001 — st.image 對無法解碼的內容拋的例外型別不固定
        logger.error("縮圖顯示失敗，技術細節：%r", exc)
        st.session_state.upload_warning = vision.USER_MESSAGE_BAD_FORMAT
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

    st.session_state.label_form = {
        "region": region, "winery": winery, "vintage": vintage, "grape": grape,
    }

    button_label = "重新分析" if st.session_state.result else "開始分析"
    pressed = st.button(button_label, disabled=not region.strip() or vintage is None)
    return pressed, st.session_state.label_form


def run_analysis(values: dict[str, Any]) -> None:
    """按鈕按下後呼叫，用 `st.status()` 顯示分段進度執行 `agent.analyze()`，結果存進
    `session_state`。

    Agent loop 背後是多次 tool call 加上報告生成，10 秒以上很常見，一個不透明的 spinner
    在手機網路下尤其讓人不安；用 `on_progress` 回呼把每個階段的白話標籤即時更新到畫面上
    （T-18）。
    """
    region = values["region"].strip()
    year = int(values["vintage"])
    logger.info("呼叫 agent.analyze()：region=%r, year=%r", region, year)
    with st.status("正在分析氣候與風味資料……", expanded=True) as status:
        st.session_state.result = agent.analyze(
            region=region,
            year=year,
            label_info={
                "winery": (values.get("winery") or "").strip() or None,
                "grape": (values.get("grape") or "").strip() or None,
            },
            on_progress=lambda message: status.update(label=message),
        )
        status.update(label="分析完成", state="complete")


def render_anomaly_metrics(anomaly: dict[str, Any] | None) -> None:
    """報告最上方的兩張距平卡片：生長積溫（GDD）與生長季降雨，各自的絕對值＋距平百分比。

    `pct_anomaly` 在基準線標準差為 0 時會是 `None`（`st.metric` 的 `delta` 支援 `None`，
    此時卡片仍會顯示原始數值、只是不畫上下箭頭，不會捏造一個假的百分比，條款 15）。
    """
    if not anomaly:
        return

    gdd = anomaly.get("gdd", {})
    rain = anomaly.get("season_precipitation", {})

    col1, col2 = st.columns(2)
    with col1:
        pct = gdd.get("pct_anomaly")
        st.metric(
            "生長積溫（GDD）", f"{gdd.get('vintage_value', 0):.0f}",
            delta=f"{pct:+.1f}%" if pct is not None else None,
        )
    with col2:
        pct = rain.get("pct_anomaly")
        st.metric(
            "生長季降雨量（mm）", f"{rain.get('vintage_value', 0):.0f}",
            delta=f"{pct:+.1f}%" if pct is not None else None,
        )


def render_report(result: agent.AnalysisResult) -> None:
    """依 `result.status` 分支渲染：成功顯示報告＋圖表，其餘顯示白話說明（US-4.1）。"""
    if result.status != "ok" or result.markdown is None:
        st.warning(result.user_message)
        return

    render_anomaly_metrics(result.gathered.anomaly)
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


def _build_temp_chart(
    comparison: pd.DataFrame, year_label: str, vintage_color: str, baseline_color: str
) -> go.Figure:
    """組出每月均溫折線圖，該年與 30 年平均固定用同一組配色角色。"""
    months = comparison["month_label"]
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=months, y=comparison["temp_mean_vintage"], name=year_label,
        line=dict(color=vintage_color, width=2),
    ))
    fig.add_trace(go.Scatter(
        x=months, y=comparison["temp_mean_baseline"], name="30 年平均",
        line=dict(color=baseline_color, width=2),
    ))
    fig.update_layout(
        title="每月均溫（該年 vs 30 年平均）", yaxis_title="°C",
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
    )
    return fig


def _build_rain_chart(
    comparison: pd.DataFrame, year_label: str, vintage_color: str, baseline_color: str
) -> go.Figure:
    """組出每月降雨量柱狀圖，跟均溫折線圖用同一組配色角色（該年／30 年平均語意一致）。"""
    months = comparison["month_label"]
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=months, y=comparison["precipitation_mm_vintage"], name=year_label,
        marker_color=vintage_color,
    ))
    fig.add_trace(go.Bar(
        x=months, y=comparison["precipitation_mm_baseline"], name="30 年平均",
        marker_color=baseline_color,
    ))
    fig.update_layout(
        title="每月降雨量（該年 vs 30 年平均）", yaxis_title="mm", barmode="group",
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
    )
    return fig


def render_charts(gathered: agent.GatheredData) -> None:
    """畫每月均溫折線圖與降雨柱狀圖（T-15、US-4.2），山區產區加 ERA5 高估提醒。"""
    if not gathered.region_canonical or not gathered.vintage_year:
        return

    comparison = _load_monthly_comparison(gathered.region_canonical, gathered.vintage_year)
    if comparison is None:
        st.info("這個產區與年份的氣候距平圖表資料暫時無法取得，不影響上面的風味推測報告。")
        return

    st.subheader("氣候距平圖表")
    year_label = f"{gathered.vintage_year} 年"

    # st.context.theme.type 在沒有真實瀏覽器連線時（例如 AppTest、主題切換瞬間）可能是
    # None，此時退回亮色模式的配色，不能讓圖表因此掛掉。
    theme_type = st.context.theme.type or "light"
    vintage_color = VINTAGE_COLOR[theme_type]
    baseline_color = BASELINE_COLOR[theme_type]

    st.plotly_chart(
        _build_temp_chart(comparison, year_label, vintage_color, baseline_color), width="stretch"
    )
    st.plotly_chart(
        _build_rain_chart(comparison, year_label, vintage_color, baseline_color), width="stretch"
    )

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

    with st.expander("上傳酒標", expanded=st.session_state.result is None):
        render_upload_section()
    pressed, values = render_label_form()

    if pressed:
        run_analysis(values)

    if st.session_state.result is not None:
        render_report(st.session_state.result)


if __name__ == "__main__":
    main()
