"""風味推測報告生成（T-13）。

把 `agent.py` 蒐集到的氣候距平與知識庫片段，交給 LLM 生成一份白話、有依據、有信心層級
的 Markdown 報告。只吃純量／dict／list（不吃 `agent.GatheredData`），刻意對 `agent.py`
零 import 依賴，方便用手刻的 dict 獨立單元測試，也讓 `python -m src.report` 可以跳過
tool-calling loop、單獨除錯報告 prompt 的品質。

四段輸出：風味推測、氣候摘要、限制說明由 LLM 依 system prompt 撰寫；資料來源段落刻意
不讓 LLM 生成，而是程式碼從實際檢索到的 metadata 決定式組裝——confidence 與出處清單
已經是結構化事實，讓 LLM 重寫這段只會多一層無謂的幻覺風險（條款 15、17）。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from typing import Any

import openai
from dotenv import load_dotenv
from openai import OpenAI

from src import climate, retrieval

logger = logging.getLogger(__name__)

# --- 常數設定 -----------------------------------------------------------------

REPORT_MODEL = "gpt-4o-mini"
REQUEST_TIMEOUT_SECONDS = 30.0

# 單一知識片段內文放進 prompt 的字數上限，控制 token 成本（條款 19）。
MAX_CHUNK_EXCERPT_CHARS = 600

# T-06／T-08 已知的方法論限制：ERA5 重分析資料在這些山區地形容易高估降雨量。
MOUNTAIN_RAINFALL_BIAS_REGIONS = frozenset({"Barolo", "Central Otago", "Marlborough"})

USER_MESSAGE_NO_API_KEY = "系統尚未設定 OpenAI API Key，請聯絡開發者。"
USER_MESSAGE_GENERATION_FAILED = "報告生成暫時無法使用，請稍後再試一次。"

_CITATION_PATTERN = re.compile(r"\[([A-Za-z0-9_]+)\]")


# --- 例外 -----------------------------------------------------------------------


class ReportGenerationError(Exception):
    """報告生成失敗。

    訊息分兩層（條款 18），與 `climate.ClimateDataError`／`retrieval.RetrievalError`／
    `vision.VisionError` 是同一種模式，但刻意獨立定義——報告生成是自己的關注點，跟其他
    模組不互相依賴。

    Attributes:
        user_message: 給使用者看的白話訊息。
        technical_detail: 給開發者除錯用的技術細節。
    """

    def __init__(self, user_message: str, technical_detail: str = "") -> None:
        super().__init__(technical_detail or user_message)
        self.user_message = user_message
        self.technical_detail = technical_detail


# --- System prompt --------------------------------------------------------------

REPORT_SYSTEM_PROMPT = """\
你是 TerroirTeller 的風味推測報告生成器。你的輸出是給一般消費者看的脈絡解讀，根據氣候
資料與知識庫片段做出有依據的推測，不是專家評鑑，也不是預言。

全文只能使用繁體中文（台灣慣用詞與用字），絕對不要出現任何簡體字或中國大陸慣用語。

## 輸出格式

依序輸出三個標題完全一致的 Markdown 段落，不要輸出其他標題：

## 風味推測
## 氣候摘要
## 限制說明

風味推測放在最前面，因為那是使用者最關心的結論；氣候摘要是支撐這個結論的證據，放在
後面。絕對不要自己輸出「資料來源」這個標題或任何列出引用清單的段落——那段會由系統在
你的輸出之後自動附加，你自己加會造成重複。

## 保留措辭

風味推測全程只能用「可能偏向」「傾向於」「相對可能」「通常呈現」這類保留措辭，禁止
「絕對是」「一定會」「必定」等斷言句。

## 引用規則

風味推測的每一段只要引用了知識片段的內容，句末要加上 `[chunk_id]` 標記，chunk_id 必須
是使用者訊息裡「知識庫片段」區塊實際列出的 id，不可以自己編一個。如果某個說法在提供的
知識片段裡找不到支持，就不要在風味推測寫這個說法，改成在限制說明裡誠實說明依據不足。

## 信心層級

每個知識片段都標了 confidence（high／medium／low），這是知識庫原本的信心評級，不要
自己重新評分。主要依據 confidence 為 low 或 medium 的片段寫成的段落，措辭要比依據 high
片段的段落更保留，例如可以加一句「這方面的證據相對有限」。

## 限制說明必須包含

- 一定要提到「採收前30天降雨」是用生長季結束日往前推算的代理指標，不是實際採收日的
  資料。
- 只有當使用者訊息裡的產區「正好」是 Barolo、Central Otago、Marlborough 這三個之一時，
  才提到 ERA5 氣候資料在山區容易高估降雨量、大約是實測值的兩倍；其他任何產區都不要提
  ERA5 或山區降雨高估這件事，不要因為聯想到「山」或「地形」就主動延伸這個說法。
- 只有在使用者訊息的氣候距平資料明確寫「目前無法取得」時，才需要說明沒有氣候資料可以
  參考、不能編造任何溫度或降雨數字；如果訊息裡已經有具體的距平數字，就不要無中生有地
  提起「資料無法取得」這件事。

## 絕對不要輸出的內容

即使脈絡看起來合理，也不要主動補充以下內容：

1. 通路、庫存、價格
2. 評分或排名，包括引用外部評鑑分數（如 Parker Points）
3. 「好不好喝」「值不值得買」之類的價值判斷，只描述風味特徵的傾向
4. 搭餐配對建議
5. 具體的陳年年份或窗口預測，只能泛稱風味傾向
6. 發酵溫度、橡木桶種類等釀造工藝細節
7. 針對特定酒莊的個別評論，只談產區共通特性
8. 這一項系統呼叫你之前已經用程式檢查過產區是否在支援清單內，這裡不需要額外處理
9. 清酒、啤酒、烈酒等非葡萄酒酒類
10. 醫療或酒精攝取建議

即使將來使用者能自由提問，以上限制同樣適用於任何回答。

## 寫作風格

用自然、有觀點但保留措辭的繁體中文散文寫作，避免以下這些讓文字讀起來像罐頭 AI 輸出的
痕跡：

- 不要用「專家認為」「研究顯示」這類沒有具體來源的說法，每個論點都要有真正的
  `[chunk_id]` 支持
- 不要每段都湊成三點列舉，兩點或四點都比固定三點自然
- 不要濫用破折號
- 不要用粗體字當小標題塞進條列裡
- 不要用「此外」當每段的固定開場轉折詞，同一份報告裡最多出現一次，其餘轉折用文句本身
  的邏輯銜接
- 結尾不要寫「希望這對您有幫助」之類的客服式收尾
"""


# --- Context 組裝 -----------------------------------------------------------------


def _build_climate_context(anomaly: dict[str, Any] | None) -> str:
    """把氣候距平資料組成給 LLM 看的文字區塊，沒有資料時誠實說明（條款 15）。

    Args:
        anomaly: `dispatch_query_climate_anomaly()` 的成功結果，或 `None`／含 `error`。

    Returns:
        給 LLM 參考的氣候資料文字區塊。
    """
    if not anomaly or anomaly.get("error"):
        detail = f"（原因：{anomaly['user_message']}）" if anomaly and anomaly.get("error") else ""
        return (
            f"氣候距平資料目前無法取得{detail}。沒有任何氣候數字可以引用，氣候摘要與"
            "限制說明都必須誠實反映這一點，不可以編造溫度、降雨或距平百分比。"
        )

    lines = []
    summary = anomaly.get("summary")
    if summary:
        lines.append(summary)
    lines.append(f"基準線區間：{anomaly['baseline_start_year']}–{anomaly['baseline_end_year']} 年")

    for metric_key, label in (
        ("gdd", "GDD（生長積溫）"),
        ("season_precipitation", "生長季總降雨"),
        ("pre_harvest_precipitation", "採收前30天降雨代理值"),
    ):
        metric = anomaly[metric_key]
        pct_str = f"{metric['pct_anomaly']:+.1f}%" if metric["pct_anomaly"] is not None else "無法計算"
        z_str = f"{metric['z_score']:+.2f}" if metric["z_score"] is not None else "無法計算"
        lines.append(f"{label}：距平 {pct_str}，z-score {z_str}")

    lines.append(
        f"採收前降雨代理值計算區間：{anomaly['harvest_proxy_window_start']} 至 "
        f"{anomaly['harvest_proxy_window_end']}（用生長季結束日往前推算，非實際採收日）"
    )
    return "\n".join(lines)


def _parse_sources(raw: Any) -> list[dict[str, Any]]:
    """還原 metadata 裡的 `sources` 欄位（`retrieval.py` 匯入時已 `json.dumps()` 過）。"""
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _format_source(source: dict[str, Any]) -> str:
    """把單筆 `sources` dict 轉成一句精簡的出處文字。"""
    label = source.get("author") or source.get("title") or "未知出處"
    year = source.get("year")
    return f"{label}（{year}）" if year else str(label)


def _build_knowledge_context(
    knowledge_hits: list[dict[str, Any]],
) -> tuple[str, dict[str, dict[str, Any]]]:
    """把檢索結果組成給 LLM 看的知識片段文字區塊。

    Args:
        knowledge_hits: `dispatch_query_climate_anomaly()`／`query_climate_knowledge()`
            回傳的 `results` 列表，每筆含 `id`、`document`、`metadata`、`distance`。

    Returns:
        `(context_text, hit_lookup)`，`hit_lookup` 供 `_build_sources_section()` 組裝
        決定式的資料來源段落使用。
    """
    if not knowledge_hits:
        return (
            "沒有檢索到相關的知識庫片段，風味推測段落不能引用任何 [chunk_id]，"
            "必須誠實說明目前依據有限。",
            {},
        )

    hit_lookup: dict[str, dict[str, Any]] = {}
    blocks = []
    for hit in knowledge_hits:
        chunk_id, metadata, document = hit["id"], hit["metadata"], hit["document"]
        hit_lookup[chunk_id] = metadata
        excerpt = (
            document if len(document) <= MAX_CHUNK_EXCERPT_CHARS
            else document[:MAX_CHUNK_EXCERPT_CHARS] + "…"
        )
        sources_str = "、".join(
            _format_source(s) for s in _parse_sources(metadata.get("sources", "[]"))[:2]
        ) or "無出處資訊"
        topic_label = metadata.get("topic") or metadata.get("rule_type") or metadata.get("layer", "")
        blocks.append(
            f"[{chunk_id}]（confidence: {metadata.get('confidence', 'unknown')}｜{topic_label}）\n"
            f"{excerpt}\n出處：{sources_str}"
        )
    return "\n\n".join(blocks), hit_lookup


def _build_user_message(
    region_canonical: str,
    region_zh: str,
    vintage_year: int | None,
    climate_context: str,
    knowledge_context: str,
    label_info: dict[str, Any] | None,
) -> str:
    """組出餵給報告生成 LLM 的 user 訊息。"""
    lines = [f"產區：{region_canonical}（{region_zh}）"]
    lines.append(f"年份：{vintage_year}" if vintage_year else "年份：未知（酒標未能辨識出年份）")
    if label_info:
        winery, grape = label_info.get("winery"), label_info.get("grape")
        if winery:
            lines.append(f"酒莊（僅供參考，報告不得評論此酒莊個別品質）：{winery}")
        if grape:
            lines.append(f"品種：{grape}")
    lines.append("\n=== 氣候距平資料 ===")
    lines.append(climate_context)
    lines.append("\n=== 知識庫片段（僅能引用以下列出的 id） ===")
    lines.append(knowledge_context)
    return "\n".join(lines)


# --- LLM 呼叫 ---------------------------------------------------------------------


def _get_client() -> OpenAI:
    """建立 OpenAI client，讀 `.env` 的 `OPENAI_API_KEY`。

    Returns:
        已設定逾時的 OpenAI client。

    Raises:
        ReportGenerationError: 環境變數 `OPENAI_API_KEY` 未設定。
    """
    load_dotenv()
    if not os.environ.get("OPENAI_API_KEY"):
        raise ReportGenerationError(USER_MESSAGE_NO_API_KEY, "環境變數 OPENAI_API_KEY 未設定")
    return OpenAI(timeout=REQUEST_TIMEOUT_SECONDS)


def _call_report_llm(client: OpenAI, user_message: str) -> str:
    """呼叫報告生成 LLM，回傳三段 Markdown 內文（不含資料來源段落）。

    不用 Structured Outputs——輸出是自由格式 Markdown 散文，不是固定 schema，普通 chat
    completion 就是對的工具。

    Raises:
        ReportGenerationError: API 呼叫失敗。
    """
    try:
        response = client.chat.completions.create(
            model=REPORT_MODEL,
            messages=[
                {"role": "system", "content": REPORT_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
        )
    except openai.OpenAIError as exc:
        logger.error("報告生成 API 呼叫失敗：%s", exc, exc_info=True)
        raise ReportGenerationError(
            USER_MESSAGE_GENERATION_FAILED, f"chat.completions.create() 失敗：{exc!r}"
        ) from exc
    return response.choices[0].message.content or ""


# --- 引用抽取與資料來源組裝 ---------------------------------------------------------


def _extract_cited_ids(body_markdown: str, known_ids: set[str]) -> list[str]:
    """抽取 body 內出現過、且確實存在於檢索結果的 `[chunk_id]` 標記，保留首次出現順序。

    模型手滑編出來、不在檢索結果內的標記直接丟棄並記 log warning，絕不讓假引用流到
    使用者畫面上。
    """
    all_matches = _CITATION_PATTERN.findall(body_markdown)
    cited: list[str] = []
    for match in all_matches:
        if match in known_ids and match not in cited:
            cited.append(match)

    dropped = set(all_matches) - known_ids
    if dropped:
        logger.warning("報告內出現不存在於檢索結果的引用標記，已捨棄：%s", dropped)
    return cited


def _build_sources_section(
    cited_ids: list[str],
    hit_lookup: dict[str, dict[str, Any]],
    anomaly: dict[str, Any] | None,
) -> str:
    """決定式組裝「資料來源」段落，不讓 LLM 生成（理由見模組 docstring）。"""
    lines = []
    for chunk_id in cited_ids:
        metadata = hit_lookup[chunk_id]
        sources_str = "、".join(
            _format_source(s) for s in _parse_sources(metadata.get("sources", "[]"))[:2]
        ) or "無出處資訊"
        lines.append(
            f"- {chunk_id}｜{metadata.get('tags', '')}"
            f"（confidence: {metadata.get('confidence', 'unknown')}｜出處：{sources_str}）"
        )

    if anomaly and not anomaly.get("error"):
        lines.append(
            "- 氣候資料｜Open-Meteo Historical Weather API (ERA5)，"
            f"{anomaly['baseline_start_year']}–{anomaly['baseline_end_year']} 年基準線"
        )

    if not lines:
        lines.append("- 本次報告未能引用任何具體知識庫片段或氣候資料。")
    return "\n".join(lines)


# --- 限制說明保底檢查 ---------------------------------------------------------------

_PRE_HARVEST_PROXY_CAVEAT = (
    "「採收前30天降雨」是用生長季結束日往前推算的代理指標，不是實際採收日的降雨資料。"
)
_MOUNTAIN_RAINFALL_CAVEAT_TEMPLATE = (
    "{region} 地形起伏較大，ERA5 氣候資料在山區容易高估降雨量（約為實測值的兩倍），"
    "這裡的降雨距平數字可以保留一定的懷疑空間。"
)


def _append_to_limitations(body_markdown: str, caveat: str) -> str:
    """把一句提醒插進 `## 限制說明` 段落開頭；該標題不存在時整段補上。"""
    marker = "## 限制說明"
    if marker not in body_markdown:
        return body_markdown.rstrip() + f"\n\n{marker}\n\n{caveat}\n"
    return body_markdown.replace(marker, f"{marker}\n\n{caveat}", 1)


def _ensure_limitation_caveats(body_markdown: str, region_canonical: str) -> str:
    """防禦性檢查：確認方法論限制的必寫提醒真的出現在限制說明裡，缺席就補上。

    這是硬性揭露要求（T-06／T-08 已知限制），單靠 prompt 遵從度有風險，補一行 fallback
    成本很低（belt-and-suspenders）。
    """
    result = body_markdown
    if "代理" not in result:
        result = _append_to_limitations(result, _PRE_HARVEST_PROXY_CAVEAT)
    if region_canonical in MOUNTAIN_RAINFALL_BIAS_REGIONS and "ERA5" not in result:
        result = _append_to_limitations(
            result, _MOUNTAIN_RAINFALL_CAVEAT_TEMPLATE.format(region=region_canonical)
        )
    return result


# --- 公開函式 -----------------------------------------------------------------------


def generate_report(
    region_canonical: str,
    region_zh: str,
    vintage_year: int | None,
    anomaly: dict[str, Any] | None,
    knowledge_hits: list[dict[str, Any]],
    label_info: dict[str, Any] | None = None,
) -> str:
    """生成風味推測報告的 Markdown 全文（T-13、US-3.3）。

    Args:
        region_canonical: 產區正式英文名稱。
        region_zh: 產區中文名稱。
        vintage_year: 酒標年份，未知時為 `None`。
        anomaly: `query_climate_anomaly` 工具的成功結果，缺資料或出錯時為 `None`。
        knowledge_hits: 檢索到的知識庫片段列表，可能為空。
        label_info: 酒標辨識結果（酒莊、品種等），非圖片流程時為 `None`。

    Returns:
        含「風味推測」「氣候摘要」「限制說明」「資料來源」四段的 Markdown 全文。

    Raises:
        ReportGenerationError: API Key 未設定或 LLM 呼叫失敗。
    """
    climate_context = _build_climate_context(anomaly)
    knowledge_context, hit_lookup = _build_knowledge_context(knowledge_hits)
    user_message = _build_user_message(
        region_canonical, region_zh, vintage_year, climate_context, knowledge_context, label_info
    )

    client = _get_client()
    body = _call_report_llm(client, user_message)
    body = _ensure_limitation_caveats(body, region_canonical)

    cited_ids = _extract_cited_ids(body, set(hit_lookup))
    sources_section = _build_sources_section(cited_ids, hit_lookup, anomaly)
    return f"{body.strip()}\n\n## 資料來源\n\n{sources_section}\n"


# --- CLI ------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """建立 CLI 參數解析器。"""
    parser = argparse.ArgumentParser(
        prog="python -m src.report",
        description="風味推測報告生成測試（T-13），跳過 agent.py 的 tool-calling loop",
    )
    parser.add_argument("--region", required=True, help="產區名稱或別名，例：Bordeaux")
    parser.add_argument("--year", type=int, required=True, help="酒標年份，例：2019")
    parser.add_argument("--n-results", type=int, default=3, help="檢索知識片段筆數上限，預設 3")
    parser.add_argument("--verbose", action="store_true", help="顯示 DEBUG 等級的開發者 log")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 進入點：直接用 `climate`／`retrieval` 組資料，跳過 agentic loop。

    Args:
        argv: 命令列參數；`None` 表示直接讀 `sys.argv`。

    Returns:
        行程結束碼，0 為成功、1 為可預期的資料錯誤、2 為參數用法錯誤。
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        region = climate.find_region(args.region)
        season = climate.fetch_season_climate(args.region, args.year)
        baseline = climate.get_baseline(args.region)
        anomaly_obj = climate.compute_climate_anomaly(season, baseline)
        anomaly = anomaly_obj.to_dict()
        anomaly["summary"] = climate.format_anomaly_summary(anomaly_obj)
        knowledge_hits = retrieval.query_climate_knowledge(
            anomaly["summary"], n_results=args.n_results, region_canonical=region["region_canonical"]
        )
        markdown = generate_report(
            region_canonical=region["region_canonical"],
            region_zh=region["region_zh"],
            vintage_year=args.year,
            anomaly=anomaly,
            knowledge_hits=knowledge_hits,
        )
    except (climate.ClimateDataError, retrieval.RetrievalError, ReportGenerationError) as exc:
        # 條款 18：使用者只看到白話訊息，技術細節寫進 log。
        logger.error("技術細節：%s", exc.technical_detail)
        print(f"\n{exc.user_message}")
        return 1

    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
