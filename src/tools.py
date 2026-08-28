"""OpenAI function calling tools 定義（T-11）。

每個 tool 定義兩部分：(1) OpenAI function-calling 用的 JSON schema（純資料，供 T-12
Agent orchestrator 的 `tools=[...]` 參數使用）；(2) 對應的 Python dispatch 函式，接收
LLM 傳入的參數、呼叫底層模組（`climate`／`retrieval`／`vision`），回傳
JSON-serializable 的 dict 結果。

錯誤處理原則：dispatch 函式一律捕捉底層模組的自訂例外（`ClimateDataError`／
`RetrievalError`／`VisionError` 及其子類別），轉成 `{"error": True, "error_type": ...,
"user_message": ...}` 的 dict 回傳，**絕不讓例外往外拋**——tool 呼叫結果要餵回給 LLM 當
對話的一部分，拋出未捕捉的例外會讓整個 orchestrator 流程中斷，而不是讓 LLM 有機會根據
錯誤內容組出得體的回應（例如「這個產區不在系統支援範圍內，你要不要換一個？」）。

`check_region_validity` 是唯一的例外：它的 `RegionNotFoundError` 代表「查詢成功、答案是
不支援」而非「工具本身壞了」，所以刻意不走 `_error_result()`，回傳
`{"valid": False, "reason": ...}` 而不是 `{"error": True, ...}`——這個 shape 差異是刻意
設計，不是疏漏，日後不要把兩者合併成同一種格式。

Schema 的 `name` 用英文 snake_case（LLM function-calling 呼叫慣例），`description` 用
繁體中文——OpenAI function calling 對 description 語言沒有限制，用中文描述能讓模型在
中文對話情境下的呼叫判斷（何時該呼叫哪個工具）更準確，這是刻意選擇。

`query_terroir_knowledge` 的 description 明確寫「僅在使用者明確問到風土或產區比較時才
呼叫」，把條款 36 的兩層檢索防汙染規則編碼進提示文字，這是 T-12 的 LLM 避免從氣候推論
漂移成產區介紹的主要機制。

**schema 參數與注入參數的邊界**（T-16 後續修正）：dispatch 函式的參數不一定都要出現在
對應的 schema 裡。`dispatch_query_climate_knowledge` 的 `temperature_direction` 就刻意
不宣告在 `CLIMATE_KNOWLEDGE_SCHEMA`——該 schema 有 `additionalProperties: False`，欄位
不在裡面模型就送不出來，這個參數只能由 `agent._inject_derived_arguments()` 從已算好的
氣候距平注入。分界線是：**LLM 判斷出來的參數走 schema，程式算出來的事實走注入**。氣候
的冷暖方向是 GDD 距平算出來的既定事實，讓模型用語意去猜正是 T-16 驗證中偏涼年份被檢索
到偏暖規則的根因。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from src import climate, retrieval, vision

logger = logging.getLogger(__name__)


# --- 錯誤轉換共用 helper -------------------------------------------------------


def _error_result(exc: Exception, error_type: str) -> dict[str, Any]:
    """把自訂例外轉成回傳給 LLM 的錯誤 dict，不外洩 technical_detail（條款 18）。

    Args:
        exc: 底層模組拋出的自訂例外（需有 `user_message`／`technical_detail` 屬性）。
        error_type: 錯誤來源標籤，供 LLM／log 判斷是哪個 tool 失敗。

    Returns:
        `{"error": True, "error_type": ..., "user_message": ...}`。
    """
    user_message = getattr(exc, "user_message", str(exc))
    technical_detail = getattr(exc, "technical_detail", repr(exc))
    logger.error("%s 失敗：%s", error_type, technical_detail)
    return {"error": True, "error_type": error_type, "user_message": user_message}


# --- Tool 1：氣候距平查詢 -------------------------------------------------------

CLIMATE_ANOMALY_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "query_climate_anomaly",
        "description": (
            "查詢指定產區與年份的氣候距平（GDD、生長季降雨、採收前降雨相對於30年基準線的"
            "百分比與z-score），用於推論該年份的氣候特徵。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "region_name": {
                    "type": "string",
                    "description": "產區名稱或別名，例如 Bordeaux 或波爾多",
                },
                "vintage_year": {
                    "type": "integer",
                    "description": "酒標上的年份",
                },
            },
            "required": ["region_name", "vintage_year"],
            "additionalProperties": False,
        },
    },
}


def dispatch_query_climate_anomaly(region_name: str, vintage_year: int) -> dict[str, Any]:
    """`query_climate_anomaly` 工具的實際執行邏輯。

    依序呼叫 `climate.fetch_season_climate()`、`climate.get_baseline()`、
    `climate.compute_climate_anomaly()`，成功時交給 `climate.build_anomaly_payload()`
    補上 `summary`、`temperature_direction`、`direction_query` 三個欄位。

    `temperature_direction` 會被 `agent._update_gathered_data()` 一起存進
    `gathered.anomaly`，讓 agent 迴圈在後續呼叫 `query_climate_knowledge` 時，能用程式
    注入的方向取代 LLM 自己猜的方向（T-16 後續修正）。

    Args:
        region_name: 產區名稱或別名。
        vintage_year: 酒標上的年份。

    Returns:
        成功時為距平結果 dict（含 `summary`、`temperature_direction`、`direction_query`）；
        失敗時為 `{"error": True, ...}`。
    """
    try:
        season = climate.fetch_season_climate(region_name, vintage_year)
        baseline = climate.get_baseline(region_name)
        anomaly = climate.compute_climate_anomaly(season, baseline)
    except climate.ClimateDataError as exc:
        return _error_result(exc, "climate_anomaly")
    return climate.build_anomaly_payload(anomaly)


# --- Tool 2：氣候知識檢索 -------------------------------------------------------

CLIMATE_KNOWLEDGE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "query_climate_knowledge",
        "description": (
            "檢索氣候規則與產區氣候相關的知識庫片段（不含風土、產區比較等非氣候內容），"
            "用於支持氣候異常到風味推論的論述並附上引用來源。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query_text": {
                    "type": "string",
                    "description": "查詢文字，例如「偏暖偏乾」",
                },
                "n_results": {
                    "type": "integer",
                    "description": "回傳筆數上限，預設 5",
                    "default": 5,
                },
                "region_canonical": {
                    "type": ["string", "null"],
                    "description": "限定產區的正式英文名稱，不限定時為 null",
                },
            },
            "required": ["query_text"],
            "additionalProperties": False,
        },
    },
}


def dispatch_query_climate_knowledge(
    query_text: str,
    n_results: int = 5,
    region_canonical: str | None = None,
    temperature_direction: str | None = None,
) -> dict[str, Any]:
    """`query_climate_knowledge` 工具的實際執行邏輯，包裝 `retrieval.query_climate_knowledge()`。

    Args:
        query_text: 查詢文字。
        n_results: 回傳筆數上限。
        region_canonical: 限定產區的正式英文名稱，不限定時為 `None`。
        temperature_direction: 氣候的溫度方向（`"warmer"`／`"cooler"`）。**刻意不在
            `CLIMATE_KNOWLEDGE_SCHEMA` 裡宣告**，所以 LLM 送不出這個參數——它由
            `agent._inject_derived_arguments()` 從已蒐集的距平資料注入。溫度方向是算出來
            的事實，不是模型該判斷的事情（T-16 後續修正）。

    Returns:
        成功時為 `{"results": [...]}`；失敗時為 `{"error": True, ...}`。
    """
    try:
        hits = retrieval.query_climate_knowledge(
            query_text, n_results, region_canonical, temperature_direction
        )
    except retrieval.RetrievalError as exc:
        return _error_result(exc, "climate_knowledge_retrieval")
    return {"results": hits}


# --- Tool 3：風土／產區比較知識檢索 ------------------------------------------------

TERROIR_KNOWLEDGE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "query_terroir_knowledge",
        "description": (
            "檢索風土、產區比較、品種介紹等不限主題的知識庫片段，僅在使用者明確問到風土"
            "或產區比較時才呼叫此工具（不用於一般氣候推論主線）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query_text": {
                    "type": "string",
                    "description": "查詢文字",
                },
                "n_results": {
                    "type": "integer",
                    "description": "回傳筆數上限，預設 5",
                    "default": 5,
                },
                "region_canonical": {
                    "type": ["string", "null"],
                    "description": "限定產區的正式英文名稱，不限定時為 null",
                },
            },
            "required": ["query_text"],
            "additionalProperties": False,
        },
    },
}


def dispatch_query_terroir_knowledge(
    query_text: str, n_results: int = 5, region_canonical: str | None = None
) -> dict[str, Any]:
    """`query_terroir_knowledge` 工具的實際執行邏輯，包裝 `retrieval.query_all_knowledge()`。

    Args:
        query_text: 查詢文字。
        n_results: 回傳筆數上限。
        region_canonical: 限定產區的正式英文名稱，不限定時為 `None`。

    Returns:
        成功時為 `{"results": [...]}`；失敗時為 `{"error": True, ...}`。
    """
    try:
        hits = retrieval.query_all_knowledge(query_text, n_results, region_canonical)
    except retrieval.RetrievalError as exc:
        return _error_result(exc, "terroir_knowledge_retrieval")
    return {"results": hits}


# --- Tool 4：酒標辨識 -----------------------------------------------------------

LABEL_RECOGNITION_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "recognize_wine_label",
        "description": (
            "辨識酒標照片，取得產區、酒莊、年份、品種四個欄位。不確定的欄位會回傳 null，"
            "不會猜測。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "image_path": {
                    "type": "string",
                    "description": "本機圖片檔案路徑",
                },
            },
            "required": ["image_path"],
            "additionalProperties": False,
        },
    },
}


def dispatch_recognize_wine_label(image_path: str) -> dict[str, Any]:
    """`recognize_wine_label` 工具的實際執行邏輯，包裝 `vision.recognize_label()`。

    Args:
        image_path: 本機圖片檔案路徑。

    Returns:
        成功時為 `LabelInfo.to_dict()`；失敗時為 `{"error": True, ...}`。
    """
    try:
        label_info = vision.recognize_label(image_path)
    except vision.VisionError as exc:
        return _error_result(exc, "label_recognition")
    return label_info.to_dict()


# --- Tool 5：產區合法性檢查 ------------------------------------------------------

REGION_VALIDITY_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "check_region_validity",
        "description": (
            "檢查產區名稱是否在系統支援的20個產區清單內。用於判斷是否要對使用者說明本"
            "系統不涵蓋該產區（拒答清單第8項），而不是憑常識回答一個系統資料庫沒有的產區。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "region_name": {
                    "type": "string",
                    "description": "使用者提到或酒標辨識出的產區名稱或別名",
                },
            },
            "required": ["region_name"],
            "additionalProperties": False,
        },
    },
}


def dispatch_check_region_validity(region_name: str) -> dict[str, Any]:
    """`check_region_validity` 工具的實際執行邏輯，包裝 `climate.find_region()`。

    刻意不使用 `_error_result()`：查無此產區是「查詢成功、答案為否」，不是工具失敗，回傳
    shape 因此跟其他 4 個 tool 不同（見模組 docstring 說明）。

    Args:
        region_name: 產區名稱或別名。

    Returns:
        合法時為 `{"valid": True, "region_canonical": ..., "region_zh": ...}`；
        不合法時為 `{"valid": False, "reason": ...}`。
    """
    try:
        region = climate.find_region(region_name)
    except climate.RegionNotFoundError as exc:
        return {"valid": False, "reason": exc.user_message}
    return {
        "valid": True,
        "region_canonical": region["region_canonical"],
        "region_zh": region["region_zh"],
    }


# --- 註冊表（供 T-12 Agent orchestrator 查表 dispatch） ----------------------------

ALL_TOOL_SCHEMAS: list[dict[str, Any]] = [
    CLIMATE_ANOMALY_SCHEMA,
    CLIMATE_KNOWLEDGE_SCHEMA,
    TERROIR_KNOWLEDGE_SCHEMA,
    LABEL_RECOGNITION_SCHEMA,
    REGION_VALIDITY_SCHEMA,
]

TOOL_DISPATCH: dict[str, Callable[..., dict[str, Any]]] = {
    "query_climate_anomaly": dispatch_query_climate_anomaly,
    "query_climate_knowledge": dispatch_query_climate_knowledge,
    "query_terroir_knowledge": dispatch_query_terroir_knowledge,
    "recognize_wine_label": dispatch_recognize_wine_label,
    "check_region_validity": dispatch_check_region_validity,
}
