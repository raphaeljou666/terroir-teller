"""知識庫向量檢索（Chroma + OpenAI text-embedding-3-small）。

對應 Backlog 任務：

* **T-09**：把兩層知識庫（`climate_rules/` + `regions/`）匯入 Chroma，並提供符合條款 36
  兩層過濾邏輯的檢索函式。

`layer` 欄位是合成的（synthesized），不是從 frontmatter 讀出——知識庫檔案本身沒有這個
欄位，是依檔案所在資料夾（`climate_rules/` 或 `regions/`）在匯入時補上的。

一個已驗證過的陷阱：chromadb 的 `get_collection()`／`get_or_create_collection()` 若不
明確傳入 `embedding_function`，會悄悄退回內建的本地模型，導致查詢向量跟寫入向量不同
空間、檢索結果錯誤但不會報錯。本模組所有 collection 存取一律走 `_get_collection()`，
不要在別處直接呼叫 `client.get_collection()`。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import chromadb
import chromadb.errors
import openai
import yaml
from chromadb.utils import embedding_functions
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = PROJECT_ROOT / "data" / "knowledge"
CLIMATE_RULES_DIR = KNOWLEDGE_DIR / "climate_rules"
REGIONS_DIR = KNOWLEDGE_DIR / "regions"
CHROMA_DIR = PROJECT_ROOT / "data" / "chroma"

COLLECTION_NAME = "terroir_knowledge"
EMBEDDING_MODEL = "text-embedding-3-small"
FRONTMATTER_DELIMITER = "---"

# 新申請、額度較低的 OpenAI 帳號 TPM（每分鐘 token 數）上限可能只有 40000，全量 98 則
# 知識庫一次送出會超過限制，因此拆成小批次呼叫 embeddings API。
EMBEDDING_BATCH_SIZE = 20
EMBEDDING_RATE_LIMIT_WAIT_SECONDS = 20.0
EMBEDDING_MAX_RETRIES = 3

USER_MESSAGE_NO_API_KEY = "系統尚未設定 OpenAI API Key，請聯絡開發者。"
USER_MESSAGE_INGEST_FAILED = "知識庫匯入暫時無法使用，請稍後再試一次。"
USER_MESSAGE_QUERY_FAILED = "知識檢索暫時無法使用，請稍後再試一次。"
USER_MESSAGE_BAD_FILE = "知識庫檔案格式有誤，請聯絡開發者。"


class RetrievalError(Exception):
    """知識檢索相關錯誤。

    訊息分兩層（條款 18）：`user_message` 是白話、可直接顯示的句子；`technical_detail`
    保留技術細節只寫進 log。與 `climate.ClimateDataError` 是同一種模式，但刻意獨立定義
    ——本模組是知識庫檢索，跟 `climate.py` 的氣候 API 是不同領域，不需要互相依賴。

    Attributes:
        user_message: 給使用者看的白話訊息。
        technical_detail: 給開發者除錯用的技術細節。
    """

    def __init__(self, user_message: str, technical_detail: str = "") -> None:
        super().__init__(technical_detail or user_message)
        self.user_message = user_message
        self.technical_detail = technical_detail


# --- Frontmatter／body 解析 ---------------------------------------------------


def parse_markdown_with_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    """手動切分知識庫 `.md` 檔的 YAML frontmatter 與內文。

    本專案沒有裝 `python-frontmatter`，用 pyyaml + 手動切字串維持最小依賴（已對全部
    98 個知識檔驗證過：每個檔案恰好有兩行單獨的 `---` 分隔線，可安全用這個方法切）。

    Args:
        path: `.md` 檔案路徑。

    Returns:
        `(frontmatter_dict, body_text)`，`body_text` 已去除前後空白。

    Raises:
        RetrievalError: 檔案缺少合法的 `---` 分隔線，或 YAML 解析失敗。
    """
    text = path.read_text(encoding="utf-8")
    parts = text.split(f"{FRONTMATTER_DELIMITER}\n", 2)
    if len(parts) < 3 or parts[0].strip() != "":
        raise RetrievalError(USER_MESSAGE_BAD_FILE, f"{path} 缺少合法的 YAML frontmatter 分隔線")
    try:
        frontmatter = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as exc:
        raise RetrievalError(USER_MESSAGE_BAD_FILE, f"{path} 的 YAML 解析失敗：{exc!r}") from exc
    return frontmatter, parts[2].strip()


# --- Metadata 扁平化 ----------------------------------------------------------


def _flatten_metadata_value(value: Any) -> str | int | float | bool:
    """把 frontmatter 欄位值轉成 Chroma metadata 接受的純量型別。

    規則（決定了之後 metadata 能不能拿來做 `where` 過濾）：

    - 純量（str/int/float/bool）：原樣保留，可用於 `where` 過濾（如 `layer`、`topic`、
      `region_canonical`、`confidence`）。
    - 純量組成的 list（如 `tags`、`main_grapes`）：逗號合併成字串存放，只供顯示／除錯用，
      不適合拿來做 `where` 過濾（合併後的字串無法對單一 tag 做精準比對）。
    - dict 或元素不是純量的 list（如 `condition`、`sources`）：整個 `json.dumps()` 成
      字串，同樣只供顯示／除錯用。

    注意 `condition` 是特例：它除了在這裡被 JSON 字串化之外，`build_chroma_metadata()`
    另外會把它的子欄位攤平成 `condition_temperature` 等可過濾的純量，見該函式說明。

    Args:
        value: frontmatter 某個欄位的原始值。

    Returns:
        Chroma metadata 接受的純量值。
    """
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list) and all(isinstance(item, (str, int, float, bool)) for item in value):
        return ", ".join(str(item) for item in value)
    return json.dumps(value, ensure_ascii=False)


def build_chroma_metadata(
    frontmatter: dict[str, Any], layer: str, source_path: Path
) -> dict[str, str | int | float | bool]:
    """把 frontmatter 轉成 Chroma metadata dict，補上合成欄位 `layer`。

    `climate_rules/` 的巢狀 `condition` 區塊會額外攤平成 `condition_temperature`、
    `condition_precipitation`、`condition_magnitude`、`condition_duration` 四個純量欄位。
    沒有這一步，`condition` 只會是一個不透明的 JSON 字串，Chroma 的 `where` 看不進字串
    內部，氣候方向就無法過濾——這正是 T-16 驗證中偏涼年份被檢索到偏暖規則的根因
    （見 `docs/07_ValidationReport.md` 根因診斷）。原本的 JSON `condition` 欄位保留不動，
    維持既有的顯示／除錯用途。

    Args:
        frontmatter: `parse_markdown_with_frontmatter()` 解析出的 dict。
        layer: `"climate_rule"` 或 `"region"`，依檔案所在資料夾合成——原始檔案沒有這個
            欄位（條款 36 的過濾依賴這個合成欄位，不是讀 frontmatter）。
        source_path: 原始檔案路徑，存成 metadata 方便除錯追查。

    Returns:
        扁平化後的 metadata dict。`None` 值的欄位（如 `sub_region: null`）直接略過不寫入；
        `id` 欄位也略過，因為它已經拿去當 Chroma 文件本身的 id，不需要在 metadata 重複一份。
    """
    metadata: dict[str, str | int | float | bool] = {
        "layer": layer,
        "source_path": str(source_path.relative_to(PROJECT_ROOT)),
    }
    for key, value in frontmatter.items():
        if value is None or key == "id":
            continue
        metadata[key] = _flatten_metadata_value(value)

    condition = frontmatter.get("condition")
    if isinstance(condition, dict):
        for sub_key, sub_value in condition.items():
            if sub_value is None:
                continue
            metadata[f"condition_{sub_key}"] = _flatten_metadata_value(sub_value)
    return metadata


# --- 檔案發現與載入 -----------------------------------------------------------


def discover_knowledge_files(sample_only: bool = False) -> list[tuple[Path, str]]:
    """列出要匯入的知識庫檔案，回傳 `(path, layer)` 清單。

    Args:
        sample_only: `True` 時只回傳 `climate_rules/` 的 10 個檔案（條款 19 小樣本流程，
            先驗證邏輯對再花錢跑全量 98 個檔案的 embedding）。

    Returns:
        依檔案路徑排序的 `(path, layer)` 清單。
    """
    files = [(path, "climate_rule") for path in sorted(CLIMATE_RULES_DIR.glob("*.md"))]
    if sample_only:
        return files
    files += [(path, "region") for path in sorted(REGIONS_DIR.glob("**/*.md"))]
    return files


def load_documents(sample_only: bool = False) -> list[dict[str, Any]]:
    """解析知識庫檔案，回傳可直接餵給 Chroma 的文件清單。

    每份 `.md` 檔整篇當一個 chunk，不按 `##` 段落再切。

    Args:
        sample_only: 同 `discover_knowledge_files()`。

    Returns:
        `[{"id", "document", "metadata"}, ...]`。

    Raises:
        RetrievalError: 檔案缺少 `id` frontmatter 欄位，或格式錯誤。
    """
    documents = []
    for path, layer in discover_knowledge_files(sample_only):
        frontmatter, body = parse_markdown_with_frontmatter(path)
        doc_id = frontmatter.get("id")
        if not doc_id:
            raise RetrievalError(USER_MESSAGE_BAD_FILE, f"{path} 缺少 frontmatter id 欄位")
        documents.append({
            "id": doc_id,
            "document": body,
            "metadata": build_chroma_metadata(frontmatter, layer, path),
        })
    return documents


# --- Chroma client／embedding function／匯入 ----------------------------------


def _get_embedding_function() -> embedding_functions.OpenAIEmbeddingFunction:
    """建立 OpenAI embedding function，讀 `.env` 的 `OPENAI_API_KEY`。

    Raises:
        RetrievalError: 環境變數 `OPENAI_API_KEY` 未設定。
    """
    load_dotenv()
    if not os.environ.get("OPENAI_API_KEY"):
        raise RetrievalError(USER_MESSAGE_NO_API_KEY, "環境變數 OPENAI_API_KEY 未設定")
    # 用 api_key_env_var 而非直接傳 api_key：後者不會被 chromadb 持久化保存，換行程重建
    # collection 時讀不到原本用的 key（見官方 DeprecationWarning）。
    return embedding_functions.OpenAIEmbeddingFunction(
        api_key_env_var="OPENAI_API_KEY", model_name=EMBEDDING_MODEL
    )


def _get_client() -> chromadb.ClientAPI:
    """建立 Chroma `PersistentClient`，資料存在 `data/chroma/`。"""
    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(CHROMA_DIR))


def _get_collection(client: chromadb.ClientAPI) -> chromadb.Collection:
    """取得（或建立）knowledge collection，一律明確帶入 embedding function。

    務必在建立與查詢 collection 時都明確傳入同一組設定的 embedding function：chromadb
    若不傳 `embedding_function`，會悄悄退回內建的本地模型，導致查詢向量跟寫入向量不同
    空間，檢索結果錯誤但不會報錯（已實測驗證過這個行為）。
    """
    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=_get_embedding_function(),
        metadata={"embedding_model": EMBEDDING_MODEL},
    )


def ingest_knowledge_base(sample_only: bool = False, reset: bool = False) -> int:
    """把知識庫檔案匯入 Chroma collection。

    Args:
        sample_only: `True` 時只匯入 `climate_rules/` 10 個檔案（條款 19：開發階段先跑
            小樣本，確認邏輯對再花錢跑全量 98 個檔案的 embedding）。
        reset: `True` 時先刪除既有 collection 再重建。

    Returns:
        實際匯入的檔案數。

    Raises:
        RetrievalError: 知識庫檔案格式錯誤，或 embedding／Chroma API 呼叫失敗。
    """
    client = _get_client()
    if reset:
        try:
            client.delete_collection(name=COLLECTION_NAME)
        except chromadb.errors.NotFoundError:
            pass
    collection = _get_collection(client)
    documents = load_documents(sample_only)

    for start in range(0, len(documents), EMBEDDING_BATCH_SIZE):
        _upsert_batch(collection, documents[start : start + EMBEDDING_BATCH_SIZE])

    logger.info("匯入完成：%d 筆（sample_only=%s, reset=%s）", len(documents), sample_only, reset)
    return len(documents)


def _upsert_batch(collection: chromadb.Collection, batch: list[dict[str, Any]]) -> None:
    """把一批文件寫入 collection，遇到 embedding API 的速率限制時等待重試。

    upsert 而非 add：重跑 ingest（例如改了某個知識檔）不會因為 id 已存在而失敗。批次大小
    受 `EMBEDDING_BATCH_SIZE` 限制——新申請、額度較低的帳號 TPM 上限偏低，一次把全部 98
    則塞進同一個 embeddings 請求會超過限制；踩到限制時等一下重試，而不是直接放棄整個匯入。

    Args:
        collection: 目標 Chroma collection。
        batch: 這一批要寫入的文件（`load_documents()` 回傳格式的子集）。

    Raises:
        RetrievalError: 重試次數用盡仍遇到速率限制，或遇到其他 embedding／Chroma 錯誤。
    """
    for attempt in range(1, EMBEDDING_MAX_RETRIES + 1):
        try:
            collection.upsert(
                ids=[doc["id"] for doc in batch],
                documents=[doc["document"] for doc in batch],
                metadatas=[doc["metadata"] for doc in batch],
            )
            return
        except openai.RateLimitError as exc:
            if attempt == EMBEDDING_MAX_RETRIES:
                raise RetrievalError(
                    USER_MESSAGE_INGEST_FAILED,
                    f"embedding 速率限制重試 {attempt} 次後仍失敗：{exc!r}",
                ) from exc
            logger.warning(
                "embedding 速率限制，%.0f 秒後重試（第 %d/%d 次）：%s",
                EMBEDDING_RATE_LIMIT_WAIT_SECONDS, attempt, EMBEDDING_MAX_RETRIES, exc,
            )
            time.sleep(EMBEDDING_RATE_LIMIT_WAIT_SECONDS)
        except (openai.OpenAIError, chromadb.errors.ChromaError) as exc:
            logger.error("知識庫匯入失敗：%s", exc, exc_info=True)
            raise RetrievalError(
                USER_MESSAGE_INGEST_FAILED, f"collection.upsert() 失敗：{exc!r}"
            ) from exc


# --- 查詢 ----------------------------------------------------------------------


def _query(query_text: str, n_results: int, where: dict[str, Any] | None) -> list[dict[str, Any]]:
    """實際呼叫 Chroma `query()`，統一輸出格式與錯誤處理。"""
    client = _get_client()
    collection = _get_collection(client)
    try:
        result = collection.query(query_texts=[query_text], n_results=n_results, where=where)
    except (openai.OpenAIError, chromadb.errors.ChromaError) as exc:
        logger.error("知識檢索失敗：%s", exc, exc_info=True)
        raise RetrievalError(USER_MESSAGE_QUERY_FAILED, f"collection.query() 失敗：{exc!r}") from exc

    hits = []
    for doc_id, document, metadata, distance in zip(
        result["ids"][0], result["documents"][0], result["metadatas"][0], result["distances"][0],
    ):
        hits.append(
            {"id": doc_id, "document": document, "metadata": metadata, "distance": distance}
        )
    return hits


def query_climate_knowledge(
    query_text: str,
    n_results: int = 5,
    region_canonical: str | None = None,
    temperature_direction: str | None = None,
) -> list[dict[str, Any]]:
    """氣候推論主線的檢索：只找 `climate_rules` 層 + `regions` 層中 `topic=climate` 的片段。

    這是條款 36 的核心防範：不過濾的話，報告會從「氣候證據推論」漂移成「產區介紹」。
    `climate_rules` 層片段沒有 `region_canonical` 欄位（它們是通用規則，適用所有產區），
    所以指定 `region_canonical` 時只會額外限制 `regions` 層那一支，`climate_rules` 層
    不受影響。

    `temperature_direction` 是 T-16 後續修正加入的方向硬過濾。溫度方向是從 GDD 距平算出
    來的既定事實，不該交給向量相似度去猜——相似度只負責在「方向正確的規則」之內排序。
    刻意只篩溫度、不篩降雨：10 則規則在「溫度 × 降雨」網格上有缺口（沒有 warm_normal），
    若連降雨一起硬篩，降雨接近平均的年份（如 2003 Bordeaux，生長季降雨距平 +0.0%）會只
    剩 `rule_hail_10` 存活。降雨方向改由查詢字串當軟訊號傳達，見
    `climate.format_direction_query()`。

    Args:
        query_text: 查詢字串，例：「生長季偏涼、降雨偏多的年份」。
        n_results: 回傳筆數上限。
        region_canonical: 指定時，`regions` 層片段額外限制在該產區；`climate_rules` 層
            不受此限制。
        temperature_direction: `"warmer"`／`"cooler"`，由 `climate.classify_gdd_direction()`
            算出。指定時 `climate_rules` 層只保留該方向與方向無關（`n/a`）的規則，後者
            涵蓋 harvest_rain／drought／hail 這類本來就跟冷暖無關的規則。`None`（方向不明）
            時完全不加這層過濾，退回純語意相似度。

    Returns:
        依相似度排序的檢索結果，每筆含 `id`、`document`、`metadata`、`distance`。
    """
    climate_only = {"$and": [{"layer": "region"}, {"topic": "climate"}]}
    if region_canonical:
        climate_only["$and"].append({"region_canonical": region_canonical})

    rule_branch: dict[str, Any] = {"layer": "climate_rule"}
    if temperature_direction:
        rule_branch = {
            "$and": [
                {"layer": "climate_rule"},
                {"condition_temperature": {"$in": [temperature_direction, "n/a"]}},
            ]
        }

    where: dict[str, Any] = {"$or": [rule_branch, climate_only]}
    hits = _query(query_text, n_results, where)

    if temperature_direction and not any(
        hit["metadata"].get("layer") == "climate_rule" for hit in hits
    ):
        # 最可能的原因是 metadata 還沒重新匯入（`condition_temperature` 欄位不存在），
        # 這種情況下查詢不會報錯，只會靜默退化成「只剩產區片段」，很難從結果看出來。
        logger.warning(
            "方向過濾（%s）後沒有命中任何 climate_rule 片段，"
            "請確認知識庫已用新版 metadata 重新匯入（python -m src.retrieval --ingest）",
            temperature_direction,
        )
    return hits


def query_all_knowledge(
    query_text: str, n_results: int = 5, region_canonical: str | None = None,
) -> list[dict[str, Any]]:
    """不限 topic 的檢索：使用者明確問風土、產區對比、品種介紹時使用。

    涵蓋 `terroir`／`comparison`／`grape_profile` 片段（條款 36 的「明確問到才檢索」出口）。

    Args:
        query_text: 查詢字串。
        n_results: 回傳筆數上限。
        region_canonical: 指定時限定該產區。

    Returns:
        依相似度排序的檢索結果。
    """
    where = {"region_canonical": region_canonical} if region_canonical else None
    return _query(query_text, n_results, where)


# --- CLI ------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """建立 CLI 參數解析器。"""
    parser = argparse.ArgumentParser(
        prog="python -m src.retrieval",
        description="Chroma 知識庫匯入與檢索測試（T-09）",
    )
    parser.add_argument("--ingest", action="store_true", help="匯入知識庫到 Chroma")
    parser.add_argument("--sample", action="store_true", help="只匯入 climate_rules 10 筆（先驗證邏輯）")
    parser.add_argument("--reset", action="store_true", help="匯入前先清空既有 collection")
    parser.add_argument("--query", help="用這段文字做語意檢索測試")
    parser.add_argument(
        "--mode", choices=["climate", "all"], default="climate",
        help="climate：只檢索氣候推論相關片段（預設）；all：不限 topic",
    )
    parser.add_argument("--region", help="限定產區（region_canonical），例：Bordeaux")
    parser.add_argument(
        "--temperature", choices=["warmer", "cooler"],
        help="限定氣候規則的溫度方向（只在 --mode climate 有作用），用於驗證方向過濾",
    )
    parser.add_argument("--n-results", type=int, default=5)
    parser.add_argument("--verbose", action="store_true", help="顯示 DEBUG 等級的開發者 log")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 進入點。

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
        if args.ingest:
            count = ingest_knowledge_base(sample_only=args.sample, reset=args.reset)
            print(f"匯入完成：{count} 筆")
        elif args.query:
            if args.mode == "climate":
                hits = query_climate_knowledge(
                    args.query, n_results=args.n_results, region_canonical=args.region,
                    temperature_direction=args.temperature,
                )
            else:
                hits = query_all_knowledge(
                    args.query, n_results=args.n_results, region_canonical=args.region
                )
            for rank, hit in enumerate(hits, start=1):
                layer = hit["metadata"].get("layer")
                topic = hit["metadata"].get("topic")
                print(f"\n[{rank}] {hit['id']}  (distance={hit['distance']:.3f})")
                print(f"    layer={layer} topic={topic}")
                print(f"    {hit['document'][:120]}...")
        else:
            parser.print_help()
            return 2
    except RetrievalError as exc:
        # 條款 18：使用者只看到白話訊息，技術細節寫進 log。
        logger.error("技術細節：%s", exc.technical_detail)
        print(f"\n{exc.user_message}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
