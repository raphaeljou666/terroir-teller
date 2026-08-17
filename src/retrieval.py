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
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RetrievalError(USER_MESSAGE_NO_API_KEY, "環境變數 OPENAI_API_KEY 未設定")
    return embedding_functions.OpenAIEmbeddingFunction(api_key=api_key, model_name=EMBEDDING_MODEL)


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

    try:
        # upsert 而非 add：重跑 ingest（例如改了某個知識檔）不會因為 id 已存在而失敗。
        collection.upsert(
            ids=[doc["id"] for doc in documents],
            documents=[doc["document"] for doc in documents],
            metadatas=[doc["metadata"] for doc in documents],
        )
    except (openai.OpenAIError, chromadb.errors.ChromaError) as exc:
        logger.error("知識庫匯入失敗：%s", exc, exc_info=True)
        raise RetrievalError(USER_MESSAGE_INGEST_FAILED, f"collection.upsert() 失敗：{exc!r}") from exc

    logger.info("匯入完成：%d 筆（sample_only=%s, reset=%s）", len(documents), sample_only, reset)
    return len(documents)


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
    query_text: str, n_results: int = 5, region_canonical: str | None = None,
) -> list[dict[str, Any]]:
    """氣候推論主線的檢索：只找 `climate_rules` 層 + `regions` 層中 `topic=climate` 的片段。

    這是條款 36 的核心防範：不過濾的話，報告會從「氣候證據推論」漂移成「產區介紹」。
    `climate_rules` 層片段沒有 `region_canonical` 欄位（它們是通用規則，適用所有產區），
    所以指定 `region_canonical` 時只會額外限制 `regions` 層那一支，`climate_rules` 層
    不受影響。

    Args:
        query_text: 查詢字串，例：「偏暖偏乾」。
        n_results: 回傳筆數上限。
        region_canonical: 指定時，`regions` 層片段額外限制在該產區；`climate_rules` 層
            不受此限制。

    Returns:
        依相似度排序的檢索結果，每筆含 `id`、`document`、`metadata`、`distance`。
    """
    climate_only = {"$and": [{"layer": "region"}, {"topic": "climate"}]}
    if region_canonical:
        climate_only["$and"].append({"region_canonical": region_canonical})
    where: dict[str, Any] = {"$or": [{"layer": "climate_rule"}, climate_only]}
    return _query(query_text, n_results, where)


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
            query_fn = query_climate_knowledge if args.mode == "climate" else query_all_knowledge
            hits = query_fn(args.query, n_results=args.n_results, region_canonical=args.region)
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
