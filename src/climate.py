"""Open-Meteo 歷史氣候資料存取與 30 年基準線快取。

對應 Backlog 任務：

* **T-06**：依產區名稱與年份取得該年生長季的每日氣候資料。
* **T-08**：抓取 1991–2020 的 30 年基準線並快取到 `data/cache/`，之後不重複打 API。

時區說明（對應 CLAUDE.md 條款 29）：Open-Meteo Archive API 的每日彙總一律以 **UTC 曆日**
為單位，本模組明確帶入 `timezone=UTC` 參數，不做當地時區轉換。所有日期字串採 `YYYY-MM-DD`
格式（條款 28）。生長季屬於「以日為尺度」的氣候統計，UTC 與當地日界線最多差幾小時，對
GDD 與降雨累積的影響可以忽略，但顯示給使用者時仍應註明資料基準為 UTC。

資料來源：Open-Meteo Historical Weather API（底層為 ECMWF ERA5 重分析資料集），
免費且不需 API Key。https://open-meteo.com/en/docs/historical-weather-api
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

logger = logging.getLogger(__name__)

# --- 常數設定 ---------------------------------------------------------------

ARCHIVE_API_URL = "https://archive-api.open-meteo.com/v1/archive"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REGIONS_PATH = PROJECT_ROOT / "data" / "regions.json"
CACHE_DIR = PROJECT_ROOT / "data" / "cache"

# 快取檔的結構版本。欄位有異動時往上加，舊快取會自動失效重抓。
CACHE_SCHEMA_VERSION = "1.0"

# 基準線區間採 WMO（世界氣象組織）標準的 30 年氣候平均期 1991–2020。
BASELINE_START_YEAR = 1991
BASELINE_END_YEAR = 2020

# 生長季定義：北半球為當年 4/1–10/31；南半球跨年，為前一年 10/1 到當年 4/30。
NORTHERN_SEASON = ((4, 1), (10, 31))
SOUTHERN_SEASON = ((10, 1), (4, 30))

# 向 Open-Meteo 索取的每日欄位，與 DailyRecord 的欄位一一對應。
DAILY_VARIABLES = (
    "temperature_2m_max",
    "temperature_2m_min",
    "temperature_2m_mean",
    "precipitation_sum",
    "sunshine_duration",
)

REQUEST_TIMEOUT_SECONDS = 60.0
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0

# ERA5 重分析資料約有 5 天延遲，接近今天的日期抓不到。
ARCHIVE_LAG_DAYS = 6

# 給使用者看的統一錯誤訊息（條款 18：使用者看白話、開發者看 log）。
USER_MESSAGE_API_FAILED = "氣候資料暫時無法取得，請稍後再試一次。"
USER_MESSAGE_NO_DATA = "這個產區與年份目前查不到氣候資料，請換一個年份看看。"


# --- 例外 -------------------------------------------------------------------


class ClimateDataError(Exception):
    """氣候資料取得失敗。

    刻意把訊息分兩層（條款 18）：`user_message` 是白話、可直接顯示在 Streamlit 介面上的
    句子；`technical_detail` 保留 API 回應、例外型別等技術細節，只寫進 log 給開發者看。

    Attributes:
        user_message: 給使用者看的白話訊息。
        technical_detail: 給開發者除錯用的技術細節。
    """

    def __init__(self, user_message: str, technical_detail: str = "") -> None:
        super().__init__(technical_detail or user_message)
        self.user_message = user_message
        self.technical_detail = technical_detail


class RegionNotFoundError(ClimateDataError):
    """查無此產區（不在 `data/regions.json` 的 20 個產區清單內）。"""


# --- 資料結構 ---------------------------------------------------------------


@dataclass(frozen=True)
class DailyRecord:
    """單一天的氣候觀測值。

    缺值一律保留 `None`，絕不用推測值或內插補（條款 15）。
    """

    date: str
    temp_max: float | None
    temp_min: float | None
    temp_mean: float | None
    precipitation_mm: float | None
    sunshine_hours: float | None


@dataclass
class SeasonClimate:
    """單一產區、單一年份的完整生長季氣候資料。

    這是 T-07（GDD 與距平計算）與 T-15（圖表）共用的資料結構。metadata 放在 dataclass
    欄位上、每日資料放 `daily`，需要做數值運算時呼叫 `to_dataframe()` 轉成 DataFrame。
    """

    region_canonical: str
    region_zh: str
    country: str
    hemisphere: str
    latitude: float
    longitude: float
    vintage_year: int
    season_start: str
    season_end: str
    timezone: str
    source: str
    is_partial: bool
    daily: list[DailyRecord] = field(default_factory=list)

    @property
    def day_count(self) -> int:
        """生長季實際取得的天數。"""
        return len(self.daily)

    def to_dataframe(self) -> pd.DataFrame:
        """轉成 pandas DataFrame，供 T-07 距平計算與 T-15 圖表使用。

        Returns:
            欄位為 `date`（datetime64）、`temp_max`、`temp_min`、`temp_mean`、
            `precipitation_mm`、`sunshine_hours` 的 DataFrame，依日期排序。
        """
        frame = pd.DataFrame([asdict(record) for record in self.daily])
        if frame.empty:
            return frame
        frame["date"] = pd.to_datetime(frame["date"], format="%Y-%m-%d")
        return frame.sort_values("date").reset_index(drop=True)

    def to_dict(self) -> dict[str, Any]:
        """轉成可直接寫入 JSON 快取的 dict。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SeasonClimate":
        """從 JSON 快取的 dict 還原成 SeasonClimate。"""
        daily = [DailyRecord(**record) for record in payload.get("daily", [])]
        return cls(**{**payload, "daily": daily})


@dataclass
class ClimateBaseline:
    """單一產區的 30 年基準線氣候，內含每個年份的完整生長季資料。

    刻意保留 30 個年份的每日原始值而不是只存平均，因為 GDD 有 `max(t - base, 0)` 的截斷
    運算，先平均再算 GDD 與先算 GDD 再平均的結果不同。留著原始值，T-07 才能算得準。
    """

    region_canonical: str
    region_zh: str
    country: str
    hemisphere: str
    latitude: float
    longitude: float
    start_year: int
    end_year: int
    timezone: str
    source: str
    fetched_at: str
    seasons: list[SeasonClimate] = field(default_factory=list)

    @property
    def year_count(self) -> int:
        """基準線實際涵蓋的年份數。"""
        return len(self.seasons)

    def to_dataframe(self) -> pd.DataFrame:
        """把 30 個生長季攤平成單一 DataFrame，多一個 `vintage_year` 欄位。"""
        frames = []
        for season in self.seasons:
            frame = season.to_dataframe()
            if frame.empty:
                continue
            frame.insert(0, "vintage_year", season.vintage_year)
            frames.append(frame)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    def to_dict(self) -> dict[str, Any]:
        """轉成可直接寫入 JSON 快取的 dict，含快取結構版本。"""
        payload = asdict(self)
        payload["cache_schema_version"] = CACHE_SCHEMA_VERSION
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ClimateBaseline":
        """從 JSON 快取的 dict 還原成 ClimateBaseline。"""
        data = {key: value for key, value in payload.items() if key != "cache_schema_version"}
        seasons = [SeasonClimate.from_dict(item) for item in data.pop("seasons", [])]
        return cls(**data, seasons=seasons)


# --- 產區查詢 ---------------------------------------------------------------


@lru_cache(maxsize=1)
def load_regions() -> tuple[dict[str, Any], ...]:
    """載入 `data/regions.json` 的產區清單。

    Returns:
        產區 dict 組成的 tuple（tuple 是為了搭配 `lru_cache` 快取，避免重複讀檔）。

    Raises:
        ClimateDataError: 檔案不存在或 JSON 格式錯誤。
    """
    try:
        with REGIONS_PATH.open(encoding="utf-8") as handle:
            regions = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("讀取產區座標表失敗：%s（%r）", REGIONS_PATH, exc)
        raise ClimateDataError(
            "產區資料讀取失敗，請聯絡開發者。",
            f"無法讀取 {REGIONS_PATH}：{exc!r}",
        ) from exc
    return tuple(regions)


def find_region(region_name: str) -> dict[str, Any]:
    """依名稱或別名找出產區資料，比對時不分大小寫、忽略前後空白。

    Args:
        region_name: 產區名稱，可用英文正式名或 `region_aliases` 內的任一別名
            （例：`"Bordeaux"`、`"波爾多"`、`"Burgundy"`）。

    Returns:
        該產區在 `data/regions.json` 內的完整 dict。

    Raises:
        RegionNotFoundError: 名稱不在 20 個產區清單內。
    """
    target = region_name.strip().casefold()
    for region in load_regions():
        candidates = [region["region_canonical"], region["region_zh"], *region["region_aliases"]]
        if any(name.strip().casefold() == target for name in candidates):
            return region

    available = "、".join(region["region_canonical"] for region in load_regions())
    logger.warning("查無產區：%r，可用產區為 %s", region_name, available)
    raise RegionNotFoundError(
        f"本系統目前不涵蓋「{region_name}」這個產區。",
        f"region_name={region_name!r} 不在 regions.json 清單內；可用產區：{available}",
    )


def region_slug(region: dict[str, Any]) -> str:
    """取得產區的檔名用代號，與知識庫資料夾名一致（例：`bordeaux`）。"""
    knowledge_dir = region.get("knowledge_dir", "")
    if knowledge_dir:
        return Path(knowledge_dir).name
    return region["region_canonical"].lower().replace(" ", "_")


# --- 生長季日期區間 ---------------------------------------------------------


def growing_season_range(vintage_year: int, hemisphere: str) -> tuple[date, date]:
    """算出指定年份的生長季起訖日期。

    北半球生長季落在當年 4 月 1 日到 10 月 31 日；南半球則跨年，從**前一年**的 10 月 1 日
    到當年的 4 月 30 日。舉例：Mendoza（門多薩，阿根廷）2019 年份的生長季是
    2018-10-01 至 2019-04-30。半球判斷寫錯會讓南半球產區整季對到反季節，是這個模組最容易
    出錯的地方。

    Args:
        vintage_year: 酒標上的年份（vintage）。
        hemisphere: 半球代號，`"N"` 為北半球、`"S"` 為南半球。

    Returns:
        `(生長季起始日, 生長季結束日)` 的 `date` 物件配對。

    Raises:
        ClimateDataError: 半球代號不是 N 或 S。
    """
    code = hemisphere.strip().upper()
    if code == "N":
        (start_month, start_day), (end_month, end_day) = NORTHERN_SEASON
        return date(vintage_year, start_month, start_day), date(vintage_year, end_month, end_day)
    if code == "S":
        (start_month, start_day), (end_month, end_day) = SOUTHERN_SEASON
        return (
            date(vintage_year - 1, start_month, start_day),
            date(vintage_year, end_month, end_day),
        )
    raise ClimateDataError(
        "產區資料有誤，請聯絡開發者。",
        f"未知的半球代號 hemisphere={hemisphere!r}，只接受 'N' 或 'S'",
    )


def latest_available_date() -> date:
    """回傳 ERA5 archive 目前大致可查到的最後一天（今天往前推幾天）。"""
    return date.today() - timedelta(days=ARCHIVE_LAG_DAYS)


# --- Open-Meteo API 呼叫 ----------------------------------------------------


def _extract_api_reason(response: httpx.Response) -> str:
    """從 Open-Meteo 的錯誤回應中取出 `reason` 欄位，取不到就回傳原始文字。"""
    try:
        return str(response.json().get("reason", response.text))
    except ValueError:
        return response.text[:500]


def _request_archive(latitude: float, longitude: float, start: date, end: date) -> dict[str, Any]:
    """實際打一次 Open-Meteo Archive API，含重試與錯誤處理（條款 14）。

    4xx 屬於參數錯誤，重試也不會好，直接拋錯；連線逾時、5xx 這類暫時性問題才重試。

    Args:
        latitude: 緯度（南半球為負值）。
        longitude: 經度（西經為負值）。
        start: 查詢起始日（含）。
        end: 查詢結束日（含）。

    Returns:
        API 回傳的原始 JSON dict，保證含有 `daily` 欄位。

    Raises:
        ClimateDataError: 重試後仍失敗，或回應格式不符預期。
    """
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": ",".join(DAILY_VARIABLES),
        "timezone": "UTC",
    }
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = httpx.get(ARCHIVE_API_URL, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
            if 400 <= response.status_code < 500:
                reason = _extract_api_reason(response)
                logger.error("Open-Meteo 參數錯誤 %s：%s", response.status_code, reason)
                raise ClimateDataError(
                    USER_MESSAGE_NO_DATA, f"HTTP {response.status_code}：{reason}"
                )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            logger.warning(
                "Open-Meteo 第 %d/%d 次呼叫失敗（%s ~ %s）：%r",
                attempt, MAX_RETRIES, params["start_date"], params["end_date"], exc,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue

        if "daily" not in payload or "time" not in payload.get("daily", {}):
            logger.error("Open-Meteo 回應缺少 daily 欄位：%s", str(payload)[:500])
            raise ClimateDataError(USER_MESSAGE_NO_DATA, f"回應缺少 daily 欄位：{str(payload)[:500]}")
        return payload

    raise ClimateDataError(
        USER_MESSAGE_API_FAILED,
        f"重試 {MAX_RETRIES} 次仍失敗（{params['start_date']} ~ {params['end_date']}）：{last_error!r}",
    )


def _parse_daily_records(payload: dict[str, Any]) -> list[DailyRecord]:
    """把 Open-Meteo 的欄位陣列回應轉成 DailyRecord 列表。

    Open-Meteo 回的是「每個變數一條等長陣列」的欄式結構，這裡轉成每天一筆的列式結構。
    缺值保留 `None`，日照秒數換算成小時（四捨五入到小數點後兩位）。
    """
    daily = payload["daily"]
    dates = daily["time"]

    def column(name: str) -> list[Any]:
        values = daily.get(name) or []
        return list(values) + [None] * (len(dates) - len(values))

    temp_max, temp_min = column("temperature_2m_max"), column("temperature_2m_min")
    temp_mean, precipitation = column("temperature_2m_mean"), column("precipitation_sum")
    sunshine = column("sunshine_duration")

    records = []
    for index, day in enumerate(dates):
        seconds = sunshine[index]
        records.append(
            DailyRecord(
                date=day,
                temp_max=temp_max[index],
                temp_min=temp_min[index],
                temp_mean=temp_mean[index],
                precipitation_mm=precipitation[index],
                sunshine_hours=round(seconds / 3600, 2) if seconds is not None else None,
            )
        )
    return records


def _build_season(
    region: dict[str, Any],
    vintage_year: int,
    start: date,
    end: date,
    records: list[DailyRecord],
    is_partial: bool,
) -> SeasonClimate:
    """把產區 metadata 與每日資料組成 SeasonClimate。"""
    return SeasonClimate(
        region_canonical=region["region_canonical"],
        region_zh=region["region_zh"],
        country=region["country"],
        hemisphere=region["hemisphere"],
        latitude=region["latitude"],
        longitude=region["longitude"],
        vintage_year=vintage_year,
        season_start=start.isoformat(),
        season_end=end.isoformat(),
        timezone="UTC",
        source="Open-Meteo Historical Weather API (ERA5)",
        is_partial=is_partial,
        daily=records,
    )


# --- T-06：單一年份生長季氣候 ----------------------------------------------


def fetch_season_climate(
    region_name: str,
    vintage_year: int,
    use_cache: bool = True,
) -> SeasonClimate:
    """取得指定產區、指定年份的生長季每日氣候資料（T-06）。

    生長季區間依 `data/regions.json` 的 `hemisphere` 欄位判斷：北半球取當年 4–10 月，
    南半球取前一年 10 月到當年 4 月。取得的資料會快取到 `data/cache/`，同樣的產區與年份
    再查一次就不會重打 API（條款 13）。

    Args:
        region_name: 產區名稱或別名（例：`"Bordeaux"`、`"波爾多"`）。
        vintage_year: 酒標上的年份。
        use_cache: 是否使用本機快取。設為 `False` 會強制重新呼叫 API 並覆寫快取。

    Returns:
        含 metadata 與每日氣候的 SeasonClimate。

    Raises:
        RegionNotFoundError: 產區不在清單內。
        ClimateDataError: 年份超出可查範圍，或 API 呼叫失敗。
    """
    region = find_region(region_name)
    start, end = growing_season_range(vintage_year, region["hemisphere"])

    available_until = latest_available_date()
    if start > available_until:
        raise ClimateDataError(
            f"{vintage_year} 年的生長季還沒開始，目前查不到氣候資料。",
            f"生長季起始日 {start.isoformat()} 晚於可查詢上限 {available_until.isoformat()}",
        )
    is_partial = end > available_until
    if is_partial:
        logger.info("%s %d 年生長季尚未結束，只取到 %s", region["region_canonical"],
                    vintage_year, available_until.isoformat())
        end = available_until

    cache_path = CACHE_DIR / f"{region_slug(region)}_{vintage_year}.json"
    if use_cache:
        cached = _read_season_cache(cache_path, vintage_year)
        if cached is not None:
            return cached

    payload = _request_archive(region["latitude"], region["longitude"], start, end)
    season = _build_season(region, vintage_year, start, end, _parse_daily_records(payload),
                           is_partial)
    if not season.daily:
        raise ClimateDataError(
            USER_MESSAGE_NO_DATA,
            f"{region['region_canonical']} {vintage_year} 年回傳 0 筆每日資料",
        )
    _write_json_cache(cache_path, season.to_dict())
    return season


def _read_season_cache(cache_path: Path, vintage_year: int) -> SeasonClimate | None:
    """讀取單一年份的生長季快取，讀不到或格式不符就回傳 `None` 讓上層重抓。"""
    payload = _read_json_cache(cache_path)
    if payload is None:
        return None
    try:
        season = SeasonClimate.from_dict(payload)
    except (TypeError, KeyError) as exc:
        logger.warning("生長季快取格式不符，將重新抓取：%s（%r）", cache_path, exc)
        return None
    if season.vintage_year != vintage_year or season.is_partial:
        # 年份對不上，或當初存的是未完結的生長季，都重抓比較安全。
        return None
    logger.info("使用快取的生長季資料：%s", cache_path)
    return season


# --- T-08：30 年基準線與快取 ------------------------------------------------


def baseline_cache_path(region: dict[str, Any]) -> Path:
    """回傳該產區基準線快取的檔案路徑。"""
    slug = region_slug(region)
    return CACHE_DIR / f"{slug}_baseline_{BASELINE_START_YEAR}_{BASELINE_END_YEAR}.json"


def get_baseline(region_name: str, refresh: bool = False) -> ClimateBaseline:
    """取得產區的 30 年基準線氣候，優先讀本機快取（T-08）。

    首次呼叫會向 Open-Meteo 抓 1991–2020 共 30 個生長季的每日資料並存成 JSON；之後同一個
    產區直接讀快取，不再打 API（條款 13）。快取路徑見 `baseline_cache_path()`。

    Args:
        region_name: 產區名稱或別名。
        refresh: 設為 `True` 會忽略既有快取重新抓取並覆寫。

    Returns:
        含 30 個生長季每日資料的 ClimateBaseline。

    Raises:
        RegionNotFoundError: 產區不在清單內。
        ClimateDataError: API 呼叫失敗。
    """
    region = find_region(region_name)
    cache_path = baseline_cache_path(region)

    if not refresh:
        baseline = _read_baseline_cache(cache_path)
        if baseline is not None:
            logger.info("使用快取的基準線資料：%s", cache_path)
            return baseline

    baseline = _build_baseline(region)
    _write_json_cache(cache_path, baseline.to_dict())
    logger.info("基準線已寫入快取：%s", cache_path)
    return baseline


def _build_baseline(region: dict[str, Any]) -> ClimateBaseline:
    """向 API 抓取整段基準線區間，再依年份切成 30 個生長季。

    刻意只打一次 API 抓完整區間（例：北半球是 1991-04-01 到 2020-10-31），再在本機切片，
    比一年打一次少 30 倍請求數，對免費 API 也比較友善。
    """
    first_start, _ = growing_season_range(BASELINE_START_YEAR, region["hemisphere"])
    _, last_end = growing_season_range(BASELINE_END_YEAR, region["hemisphere"])

    logger.info("抓取 %s 基準線 %s ~ %s", region["region_canonical"],
                first_start.isoformat(), last_end.isoformat())
    payload = _request_archive(region["latitude"], region["longitude"], first_start, last_end)
    records_by_date = {record.date: record for record in _parse_daily_records(payload)}

    seasons = []
    for year in range(BASELINE_START_YEAR, BASELINE_END_YEAR + 1):
        start, end = growing_season_range(year, region["hemisphere"])
        records = _slice_records(records_by_date, start, end)
        if not records:
            logger.warning("%s %d 年生長季無資料，已跳過", region["region_canonical"], year)
            continue
        seasons.append(_build_season(region, year, start, end, records, is_partial=False))

    if not seasons:
        raise ClimateDataError(
            USER_MESSAGE_NO_DATA,
            f"{region['region_canonical']} 基準線切片後 0 個生長季，原始回應天數："
            f"{len(records_by_date)}",
        )
    return _assemble_baseline(region, seasons)


def _slice_records(
    records_by_date: dict[str, DailyRecord],
    start: date,
    end: date,
) -> list[DailyRecord]:
    """從日期索引中取出 `start` 到 `end`（含）之間的每日資料，缺的日期直接略過。"""
    records = []
    current = start
    while current <= end:
        record = records_by_date.get(current.isoformat())
        if record is not None:
            records.append(record)
        current += timedelta(days=1)
    return records


def _assemble_baseline(
    region: dict[str, Any],
    seasons: list[SeasonClimate],
) -> ClimateBaseline:
    """把產區 metadata 與各年生長季組成 ClimateBaseline。"""
    return ClimateBaseline(
        region_canonical=region["region_canonical"],
        region_zh=region["region_zh"],
        country=region["country"],
        hemisphere=region["hemisphere"],
        latitude=region["latitude"],
        longitude=region["longitude"],
        start_year=BASELINE_START_YEAR,
        end_year=BASELINE_END_YEAR,
        timezone="UTC",
        source="Open-Meteo Historical Weather API (ERA5)",
        fetched_at=date.today().isoformat(),
        seasons=seasons,
    )


def _read_baseline_cache(cache_path: Path) -> ClimateBaseline | None:
    """讀取基準線快取，版本或年份區間對不上就回傳 `None` 讓上層重抓。"""
    payload = _read_json_cache(cache_path)
    if payload is None:
        return None
    if payload.get("cache_schema_version") != CACHE_SCHEMA_VERSION:
        logger.info("快取結構版本不符（%s），將重新抓取：%s",
                    payload.get("cache_schema_version"), cache_path)
        return None
    try:
        baseline = ClimateBaseline.from_dict(payload)
    except (TypeError, KeyError) as exc:
        logger.warning("基準線快取格式不符，將重新抓取：%s（%r）", cache_path, exc)
        return None
    if (baseline.start_year, baseline.end_year) != (BASELINE_START_YEAR, BASELINE_END_YEAR):
        return None
    return baseline


def warm_all_baselines(refresh: bool = False) -> dict[str, str]:
    """預熱全部 20 個產區的基準線快取，供 demo 前一次抓好用。

    單一產區失敗不會中斷其他產區，最後統一回報結果。

    Args:
        refresh: 設為 `True` 會忽略既有快取，全部重新抓取。

    Returns:
        以產區正式名為 key、狀態字串為 value 的 dict（`"ok（30 年）"` 或 `"失敗：..."`）。
    """
    results: dict[str, str] = {}
    for region in load_regions():
        name = region["region_canonical"]
        try:
            baseline = get_baseline(name, refresh=refresh)
        except ClimateDataError as exc:
            logger.error("預熱 %s 基準線失敗：%s", name, exc.technical_detail)
            results[name] = f"失敗：{exc.user_message}"
            continue
        results[name] = f"ok（{baseline.year_count} 年）"
    return results


# --- JSON 快取讀寫 ----------------------------------------------------------


def _read_json_cache(cache_path: Path) -> dict[str, Any] | None:
    """讀取 JSON 快取檔，檔案不存在或損毀都回傳 `None`（不拋錯，讓上層重抓）。"""
    if not cache_path.exists():
        return None
    try:
        with cache_path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("快取檔讀取失敗，將重新抓取：%s（%r）", cache_path, exc)
        return None


def _write_json_cache(cache_path: Path, payload: dict[str, Any]) -> None:
    """把資料寫入 JSON 快取。

    先寫暫存檔再 rename，避免中途中斷留下半截的壞快取。寫入失敗只記 log 不拋錯——快取寫不
    進去頂多下次多打一次 API，不該讓整個流程掛掉。
    """
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = cache_path.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        temp_path.replace(cache_path)
    except OSError as exc:
        logger.warning("快取寫入失敗（不影響本次查詢結果）：%s（%r）", cache_path, exc)


# --- CLI --------------------------------------------------------------------


def summarise_season(season: SeasonClimate) -> str:
    """把生長季資料整理成一段可直接印在終端機的摘要文字。"""
    frame = season.to_dataframe()
    lines = [
        f"產區：{season.region_canonical}（{season.region_zh}）／{season.country}",
        f"半球：{season.hemisphere}　座標：{season.latitude}, {season.longitude}",
        f"年份：{season.vintage_year}　生長季：{season.season_start} ~ {season.season_end}"
        f"（{season.day_count} 天，時區 {season.timezone}）",
        f"平均溫：{frame['temp_mean'].mean():.1f}°C　"
        f"最高溫：{frame['temp_max'].max():.1f}°C　最低溫：{frame['temp_min'].min():.1f}°C",
        f"生長季總降雨：{frame['precipitation_mm'].sum():.1f} mm　"
        f"總日照：{frame['sunshine_hours'].sum():.0f} 小時",
        f"資料來源：{season.source}",
    ]
    if season.is_partial:
        lines.append("註：這個生長季尚未結束，資料不完整。")
    return "\n".join(lines)


def _monthly_table(season: SeasonClimate) -> str:
    """產出逐月摘要表，方便人工目視檢查半球與日期區間有沒有寫錯。"""
    frame = season.to_dataframe()
    frame["月份"] = frame["date"].dt.strftime("%Y-%m")
    monthly = frame.groupby("月份").agg(
        平均溫=("temp_mean", "mean"),
        最高溫=("temp_max", "max"),
        降雨mm=("precipitation_mm", "sum"),
    ).round(1)
    return monthly.to_string()


def _run_season_command(args: argparse.Namespace) -> None:
    """CLI：印出單一年份的生長季氣候摘要。"""
    season = fetch_season_climate(args.region, args.year, use_cache=not args.refresh)
    print(summarise_season(season))
    print("\n逐月摘要（人工檢核用）：")
    print(_monthly_table(season))
    print("\n前 5 天原始資料：")
    print(season.to_dataframe().head().to_string(index=False))


def _run_baseline_command(args: argparse.Namespace) -> None:
    """CLI：抓取或讀取單一產區的 30 年基準線。"""
    baseline = get_baseline(args.region, refresh=args.refresh)
    frame = baseline.to_dataframe()
    print(f"產區：{baseline.region_canonical}（{baseline.region_zh}）　半球：{baseline.hemisphere}")
    print(f"基準線區間：{baseline.start_year}–{baseline.end_year}　"
          f"實際年數：{baseline.year_count}　總天數：{len(frame)}")
    print(f"生長季平均溫（30 年）：{frame['temp_mean'].mean():.2f}°C")
    print(f"生長季年均降雨（30 年）：{frame['precipitation_mm'].sum() / baseline.year_count:.1f} mm")
    print(f"快取檔：{baseline_cache_path(find_region(args.region))}")


def _run_warm_cache_command(args: argparse.Namespace) -> None:
    """CLI：一次預熱全部產區的基準線快取。"""
    print(f"開始預熱 {len(load_regions())} 個產區的基準線（{BASELINE_START_YEAR}–"
          f"{BASELINE_END_YEAR}）……")
    for name, status in warm_all_baselines(refresh=args.refresh).items():
        print(f"  {name:<20} {status}")


def build_parser() -> argparse.ArgumentParser:
    """建立 CLI 參數解析器。"""
    parser = argparse.ArgumentParser(
        prog="python -m src.climate",
        description="Open-Meteo 產區氣候查詢與 30 年基準線快取（T-06、T-08）",
    )
    parser.add_argument("--region", help="產區名稱或別名，例：Bordeaux、波爾多")
    parser.add_argument("--year", type=int, help="酒標年份，例：2019")
    parser.add_argument("--baseline", action="store_true",
                        help=f"抓取／顯示 {BASELINE_START_YEAR}–{BASELINE_END_YEAR} 基準線")
    parser.add_argument("--warm-cache-all", action="store_true", help="一次預熱全部產區的基準線")
    parser.add_argument("--list-regions", action="store_true", help="列出可查詢的產區")
    parser.add_argument("--refresh", action="store_true", help="忽略既有快取，強制重新抓取")
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
        if args.list_regions:
            for region in load_regions():
                print(f"{region['region_canonical']:<20}{region['region_zh']:<8}"
                      f"{region['hemisphere']}　{region['country_zh']}")
        elif args.warm_cache_all:
            _run_warm_cache_command(args)
        elif args.baseline:
            if not args.region:
                parser.error("--baseline 需要搭配 --region")
            _run_baseline_command(args)
        elif args.region and args.year:
            _run_season_command(args)
        else:
            parser.print_help()
            return 2
    except ClimateDataError as exc:
        # 條款 18：使用者只看到白話訊息，技術細節寫進 log。
        logger.error("技術細節：%s", exc.technical_detail)
        print(f"\n{exc.user_message}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
