"""Open-Meteo 歷史氣候資料存取、30 年基準線快取，以及 GDD／距平計算。

對應 Backlog 任務：

* **T-06**：依產區名稱與年份取得該年生長季的每日氣候資料。
* **T-07**：以 30 年基準線為對照，計算 GDD 與降雨的距平（百分比與 z-score 並列）。
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

# Open-Meteo 免費方案有每分鐘請求上限，超過會回 429 並要求等一分鐘。
RATE_LIMIT_STATUS = 429
RATE_LIMIT_WAIT_SECONDS = 65.0

# 批次預熱時每個產區之間的間隔。一次基準線請求要抓 30 年的每日資料，權重不低，
# 連續打很容易踩到每分鐘上限。
WARM_CACHE_DELAY_SECONDS = 10.0

# ERA5 重分析資料約有 5 天延遲，接近今天的日期抓不到。
ARCHIVE_LAG_DAYS = 6

# GDD（生長積溫）的基礎溫度，業界慣例採 10°C，超過的部分才累加。
GDD_BASE_TEMP_C = 10.0

# 「採收前降雨」用生長季結束日往前推這麼多天當代理指標。本專案沒有真實採收日資料，
# 這只是粗略代理值，不同品種、產區、年份的實際採收時間點不一樣（見條款 15 的精神：
# 沒有的資料不假裝有，代理值要在輸出中明確標註）。
HARVEST_PROXY_WINDOW_DAYS = 30

# GDD 距平要超過這個百分比，才判定該年份「偏暖」或「偏涼」；落在區間內視為方向不明。
#
# 校準依據（T-16 驗證的 5 個實測案例，見 docs/07_ValidationReport.md）：
# 2013 Bordeaux −3.63、2011 Napa −11.17、2003 Bordeaux +16.70、2018 Napa +3.48、
# 2019 Bordeaux +6.28。綁死上限的是 2018 Napa 的 +3.48%——deadband 必須小於它，該年
# 才會被正確判成偏暖。取 2.0 讓兩側都留有餘裕，5 個案例全部分類正確。
GDD_DIRECTION_DEADBAND_PCT = 2.0

# 降雨方向的 deadband 取得比溫度寬，因為降雨本身的年際變異就大得多：以 Bordeaux 的
# 30 年基準線為例，降雨的變異係數（std/mean）約 21.6%，GDD 只有約 7.9%，差了 2.7 倍。
# 同一個門檻套在兩者上，會讓降雨過度頻繁地被判出方向。
PRECIP_DIRECTION_DEADBAND_PCT = 5.0

# 給使用者看的統一錯誤訊息（條款 18：使用者看白話、開發者看 log）。
USER_MESSAGE_API_FAILED = "氣候資料暫時無法取得，請稍後再試一次。"
USER_MESSAGE_NO_DATA = "這個產區與年份目前查不到氣候資料，請換一個年份看看。"
USER_MESSAGE_QUOTA_HOUR = "氣候資料服務目前用量已滿，請一小時後再試。"
USER_MESSAGE_QUOTA_DAY = "氣候資料服務今天的用量已滿，請明天再試。"
USER_MESSAGE_INSUFFICIENT_BASELINE = "這個產區的基準線資料不足，無法計算距平，請稍後再試或聯絡開發者。"


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


class ApiQuotaExceededError(ClimateDataError):
    """已達 Open-Meteo 的每小時或每日用量上限。

    跟一般的暫時性失敗分開，是因為這種情況等幾秒鐘重試沒有意義，呼叫端應該直接停手，
    而不是硬跑完剩下的產區、每個都卡三次重試。
    """


class InsufficientBaselineDataError(ClimateDataError):
    """基準線有效年數不足（少於 2 年）或完全沒有資料，無法計算距平所需的平均與標準差。

    跟單一天缺值是不同層級的問題：單日缺值只是排除該天不計入加總（見
    `_season_climate_metrics`），不會走到這裡；只有整條基準線壞掉或年數太少、統計上算不出
    標準差時才拋這個錯。
    """


class _RateLimitedError(Exception):
    """模組內部用：Open-Meteo 回 429。`retryable` 為 True 時等一下重試就會過。"""

    def __init__(self, reason: str, retryable: bool, user_message: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
        self.user_message = user_message


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


def _classify_rate_limit(reason: str) -> _RateLimitedError:
    """判斷 429 是每分鐘上限（等一下就好）還是每小時／每日上限（等再久也沒用）。

    Open-Meteo 的 429 回應會在 `reason` 裡寫明是哪一種，例如
    `"Minutely API request limit exceeded. Please try again in one minute."`。
    """
    lowered = reason.lower()
    if "hour" in lowered:
        return _RateLimitedError(reason, retryable=False, user_message=USER_MESSAGE_QUOTA_HOUR)
    if "dai" in lowered or "day" in lowered:
        return _RateLimitedError(reason, retryable=False, user_message=USER_MESSAGE_QUOTA_DAY)
    return _RateLimitedError(reason, retryable=True, user_message=USER_MESSAGE_API_FAILED)


def _request_archive(latitude: float, longitude: float, start: date, end: date) -> dict[str, Any]:
    """實際打一次 Open-Meteo Archive API，含重試與錯誤處理（條款 14）。

    4xx 多半是參數錯誤，重試也不會好，直接拋錯。例外是 429（超過每分鐘請求上限），那是
    暫時性的，等一分鐘再試就會過。連線逾時、5xx 同樣會重試。

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
            if response.status_code == RATE_LIMIT_STATUS:
                raise _classify_rate_limit(_extract_api_reason(response))
            if 400 <= response.status_code < 500:
                reason = _extract_api_reason(response)
                logger.error("Open-Meteo 參數錯誤 %s：%s", response.status_code, reason)
                raise ClimateDataError(
                    USER_MESSAGE_NO_DATA, f"HTTP {response.status_code}：{reason}"
                )
            response.raise_for_status()
            payload = response.json()
        except _RateLimitedError as exc:
            if not exc.retryable:
                logger.error("Open-Meteo 用量上限，停止重試：%s", exc.reason)
                raise ApiQuotaExceededError(exc.user_message, f"HTTP 429：{exc.reason}") from exc
            last_error = exc
            logger.warning(
                "Open-Meteo 已達每分鐘請求上限，等 %.0f 秒後重試（第 %d/%d 次）：%s",
                RATE_LIMIT_WAIT_SECONDS, attempt, MAX_RETRIES, exc.reason,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RATE_LIMIT_WAIT_SECONDS)
            continue
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

    單一產區失敗不會中斷其他產區，但碰到每小時／每日用量上限會直接停手——那種情況繼續跑
    只是讓剩下的產區一個個卡在重試上。實測 Open-Meteo 免費方案一小時大約只夠抓 13–14 個
    產區的基準線，20 個要分兩次跑。最後統一回報結果。每個產區之間會停
    `WARM_CACHE_DELAY_SECONDS` 秒——一次基準線請求要抓 30 年的每日資料，20 個產區連續打
    會撞上免費方案的每分鐘上限。已經有快取的產區不用等，直接跳過。

    Args:
        refresh: 設為 `True` 會忽略既有快取，全部重新抓取。

    Returns:
        以產區正式名為 key、狀態字串為 value 的 dict（`"ok（30 年）"` 或 `"失敗：..."`）。
    """
    regions = load_regions()
    results: dict[str, str] = {}
    for index, region in enumerate(regions):
        name = region["region_canonical"]
        needs_fetch = refresh or _read_baseline_cache(baseline_cache_path(region)) is None
        if needs_fetch and index > 0:
            time.sleep(WARM_CACHE_DELAY_SECONDS)
        try:
            baseline = get_baseline(name, refresh=refresh)
        except ApiQuotaExceededError as exc:
            logger.error("API 用量已達上限，停止預熱：%s", exc.technical_detail)
            for pending in regions[index:]:
                results[pending["region_canonical"]] = f"未抓取：{exc.user_message}"
            break
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


# --- T-07：GDD 與距平計算 ----------------------------------------------------


@dataclass(frozen=True)
class MetricAnomaly:
    """單一氣候指標（GDD／生長季降雨／採收前降雨代理值）的距平結果。

    `vintage_missing_day_count`／`baseline_missing_day_count` 記錄計算這項指標時被排除
    的缺值天數，缺值天數本身不計入加總或平均（不當 0、不補值，呼應條款 15）。`std` 為 0
    （30 年數值完全相同）或 `mean` 為 0 時，對應的 `pct_anomaly`／`z_score` 回傳 `None`
    而不是拋錯或除以 0。
    """

    metric_name: str
    vintage_value: float
    vintage_missing_day_count: int
    baseline_mean: float
    baseline_std: float
    baseline_year_count: int
    baseline_missing_day_count: int
    baseline_years_with_missing_data: tuple[int, ...]
    pct_anomaly: float | None
    z_score: float | None


@dataclass(frozen=True)
class ClimateAnomaly:
    """單一年份 vs. 30 年基準線的完整距平結果，對應 US-2.3 與 Backlog T-07。"""

    region_canonical: str
    region_zh: str
    vintage_year: int
    baseline_start_year: int
    baseline_end_year: int
    gdd: MetricAnomaly
    season_precipitation: MetricAnomaly
    pre_harvest_precipitation: MetricAnomaly
    harvest_proxy_window_start: str
    harvest_proxy_window_end: str

    def to_dict(self) -> dict[str, Any]:
        """轉成可序列化的 dict，供之後的報告生成或快取使用。"""
        return asdict(self)


def _season_climate_metrics(
    frame: pd.DataFrame, harvest_window_days: int = HARVEST_PROXY_WINDOW_DAYS
) -> dict[str, Any]:
    """從單一年份的每日 DataFrame 算出 GDD、生長季降雨、採收前降雨代理值。

    GDD 逐日計算 `max(temp_mean - GDD_BASE_TEMP_C, 0)` 再加總；`temp_mean` 缺值的日子
    直接跳過、不計入加總（不當 0，不用其他值填補，呼應條款 15）。降雨採一樣的缺值排除
    邏輯。這個函式故意只吃「單一年份」的資料，同一份邏輯要同時用在目標年份與基準線的
    每一年，順序才不會被誤用成「先跨年平均氣溫再算 GDD」。

    Args:
        frame: 單一年份的每日資料，需含 `date`、`temp_mean`、`precipitation_mm` 欄位。
        harvest_window_days: 採收前降雨代理指標回推的天數（含當天），預設 30 天。

    Returns:
        dict，含 `gdd`、`gdd_missing_days`、`season_precip_mm`、
        `season_precip_missing_days`、`pre_harvest_precip_mm`、`pre_harvest_missing_days`、
        `pre_harvest_window_start`、`pre_harvest_window_end`（後兩者為 `YYYY-MM-DD` 字串）。

    Raises:
        ClimateDataError: `frame` 為空，沒有任何一天的資料可算。
    """
    if frame.empty:
        raise ClimateDataError(
            "這個年份沒有可用的氣候資料，無法計算距平。",
            "傳入 _season_climate_metrics() 的 DataFrame 為空",
        )

    temp_valid = frame["temp_mean"].notna()
    gdd = float(frame.loc[temp_valid, "temp_mean"].sub(GDD_BASE_TEMP_C).clip(lower=0.0).sum())
    gdd_missing_days = int((~temp_valid).sum())

    precip_valid = frame["precipitation_mm"].notna()
    season_precip_mm = float(frame.loc[precip_valid, "precipitation_mm"].sum())
    season_precip_missing_days = int((~precip_valid).sum())

    season_end_date = frame["date"].max()
    window_start = season_end_date - pd.Timedelta(days=harvest_window_days - 1)
    window = frame[frame["date"] >= window_start]
    window_valid = window["precipitation_mm"].notna()
    pre_harvest_precip_mm = float(window.loc[window_valid, "precipitation_mm"].sum())
    pre_harvest_missing_days = int((~window_valid).sum())

    return {
        "gdd": gdd,
        "gdd_missing_days": gdd_missing_days,
        "season_precip_mm": season_precip_mm,
        "season_precip_missing_days": season_precip_missing_days,
        "pre_harvest_precip_mm": pre_harvest_precip_mm,
        "pre_harvest_missing_days": pre_harvest_missing_days,
        "pre_harvest_window_start": window_start.strftime("%Y-%m-%d"),
        "pre_harvest_window_end": season_end_date.strftime("%Y-%m-%d"),
    }


def _baseline_year_metrics(baseline: ClimateBaseline) -> dict[int, dict[str, Any]]:
    """把 30 年基準線攤平後逐年計算三項指標。

    GDD 必須逐年算完再取平均，不能先把 30 年的日溫平均起來再算 GDD——`max(t - base, 0)`
    的截斷運算不能跟平均互換順序，先平均會讓冷涼產區的 GDD 被低估得更嚴重。

    Args:
        baseline: 產區的 30 年基準線。

    Returns:
        `{vintage_year: _season_climate_metrics() 的回傳 dict}`。

    Raises:
        InsufficientBaselineDataError: 基準線完全沒有可用的每日資料。
    """
    frame = baseline.to_dataframe()
    if frame.empty:
        raise InsufficientBaselineDataError(
            USER_MESSAGE_INSUFFICIENT_BASELINE,
            f"{baseline.region_canonical} 的基準線 to_dataframe() 為空",
        )
    return {
        int(year): _season_climate_metrics(group.reset_index(drop=True))
        for year, group in frame.groupby("vintage_year")
    }


def _baseline_metric_stats(values: list[float]) -> tuple[float, float, int]:
    """算出基準線某項指標的平均值、標準差、有效年數。

    標準差採母體標準差（`ddof=0`）：WMO 30 年氣候常態期的距平／z-score 慣例把這 30 年
    視為該常態期的完整母體，不是對更長期氣候的抽樣估計，因此不用 pandas 預設的
    `ddof=1`，需要明確指定。

    Args:
        values: 30 個年份（或實際年數）的單一指標數值。

    Returns:
        `(平均值, 標準差, 有效年數)`。

    Raises:
        InsufficientBaselineDataError: 有效年數少於 2，統計上無法定義標準差。
    """
    if len(values) < 2:
        raise InsufficientBaselineDataError(
            USER_MESSAGE_INSUFFICIENT_BASELINE,
            f"基準線有效年數 {len(values)} < 2，無法計算標準差",
        )
    series = pd.Series(values, dtype="float64")
    return float(series.mean()), float(series.std(ddof=0)), len(values)


def _build_metric_anomaly(
    metric_name: str,
    value_key: str,
    missing_key: str,
    vintage_metrics: dict[str, Any],
    per_year_metrics: dict[int, dict[str, Any]],
) -> MetricAnomaly:
    """組出單一指標的距平結果：百分比距平與 z-score 都算，並防呆除以 0。

    缺值年份不會被排除在平均值計算之外（依專案決策：不設排除門檻，只標註缺值），
    `baseline_years_with_missing_data` 只是告訴使用者「這些年份的計算排除了幾天缺值」，
    不影響這些年份本身是否納入平均。

    Args:
        metric_name: 指標名稱，例：`"gdd"`。
        value_key: 從 metrics dict 取數值的欄位名。
        missing_key: 從 metrics dict 取缺值天數的欄位名。
        vintage_metrics: 目標年份的 `_season_climate_metrics()` 回傳 dict。
        per_year_metrics: 基準線逐年的 `_season_climate_metrics()` 回傳 dict。

    Returns:
        `MetricAnomaly`。
    """
    values = [metrics[value_key] for metrics in per_year_metrics.values()]
    mean, std, year_count = _baseline_metric_stats(values)
    vintage_value = vintage_metrics[value_key]

    pct_anomaly = None if mean == 0 else (vintage_value - mean) / mean * 100
    z_score = None if std == 0 else (vintage_value - mean) / std

    baseline_missing = sum(metrics[missing_key] for metrics in per_year_metrics.values())
    missing_years = tuple(
        sorted(year for year, metrics in per_year_metrics.items() if metrics[missing_key] > 0)
    )

    return MetricAnomaly(
        metric_name=metric_name,
        vintage_value=vintage_value,
        vintage_missing_day_count=vintage_metrics[missing_key],
        baseline_mean=mean,
        baseline_std=std,
        baseline_year_count=year_count,
        baseline_missing_day_count=baseline_missing,
        baseline_years_with_missing_data=missing_years,
        pct_anomaly=pct_anomaly,
        z_score=z_score,
    )


def compute_climate_anomaly(season: SeasonClimate, baseline: ClimateBaseline) -> ClimateAnomaly:
    """比較單一年份氣候與 30 年基準線，算出 GDD 與降雨的距平（百分比 + z-score）。

    對應 US-2.3：量化「這年比 30 年平均暖多少、雨多少」。三項指標——GDD、生長季總降雨、
    採收前 30 天降雨（代理指標）——都同時輸出百分比距平與 z-score。

    Args:
        season: 目標年份的生長季氣候（來自 `fetch_season_climate()`）。
        baseline: 該產區的 30 年基準線（來自 `get_baseline()`）。

    Returns:
        `ClimateAnomaly`，含三項指標的距平與缺值天數統計。

    Raises:
        ClimateDataError: `season` 與 `baseline` 的產區不一致，或任一方沒有每日資料。
        InsufficientBaselineDataError: 基準線有效年數少於 2。
    """
    if season.region_canonical != baseline.region_canonical:
        raise ClimateDataError(
            "氣候資料與基準線的產區不一致，請聯絡開發者。",
            f"season.region_canonical={season.region_canonical!r} != "
            f"baseline.region_canonical={baseline.region_canonical!r}",
        )

    vintage_metrics = _season_climate_metrics(season.to_dataframe())
    per_year_metrics = _baseline_year_metrics(baseline)

    gdd = _build_metric_anomaly(
        "gdd", "gdd", "gdd_missing_days", vintage_metrics, per_year_metrics
    )
    season_precip = _build_metric_anomaly(
        "season_precipitation_mm", "season_precip_mm", "season_precip_missing_days",
        vintage_metrics, per_year_metrics,
    )
    pre_harvest = _build_metric_anomaly(
        "pre_harvest_precipitation_mm", "pre_harvest_precip_mm", "pre_harvest_missing_days",
        vintage_metrics, per_year_metrics,
    )

    return ClimateAnomaly(
        region_canonical=season.region_canonical,
        region_zh=season.region_zh,
        vintage_year=season.vintage_year,
        baseline_start_year=baseline.start_year,
        baseline_end_year=baseline.end_year,
        gdd=gdd,
        season_precipitation=season_precip,
        pre_harvest_precipitation=pre_harvest,
        harvest_proxy_window_start=vintage_metrics["pre_harvest_window_start"],
        harvest_proxy_window_end=vintage_metrics["pre_harvest_window_end"],
    )


def _describe_pct_anomaly(metric: MetricAnomaly, label: str) -> str:
    """把單一指標的百分比距平轉成一句話，例：「GDD 比 30 年平均高 12%」。"""
    if metric.pct_anomaly is None:
        return f"{label}與 30 年平均持平（基準值為 0，無法算百分比）"
    direction = "高" if metric.pct_anomaly >= 0 else "少"
    return f"{label}比 30 年平均{direction}{abs(metric.pct_anomaly):.0f}%"


def format_anomaly_summary(anomaly: ClimateAnomaly) -> str:
    """把距平結果整理成人類可讀摘要，供 CLI 與後續報告生成參考。"""
    headline = (
        f"{anomaly.region_canonical}（{anomaly.region_zh}）{anomaly.vintage_year} "
        f"{_describe_pct_anomaly(anomaly.gdd, 'GDD')}、"
        f"{_describe_pct_anomaly(anomaly.season_precipitation, '生長季降雨')}。"
    )
    harvest_line = (
        f"採收前 30 天降雨代理值（以生長季結束日 {anomaly.harvest_proxy_window_end} "
        f"往前推算，非實際採收日）：{_describe_pct_anomaly(anomaly.pre_harvest_precipitation, '')}"
    )
    lines = [headline, harvest_line]

    missing_notes = []
    for metric, label in (
        (anomaly.gdd, "GDD／溫度"),
        (anomaly.season_precipitation, "生長季降雨"),
        (anomaly.pre_harvest_precipitation, "採收前降雨"),
    ):
        if metric.vintage_missing_day_count:
            missing_notes.append(f"{label}當年缺值 {metric.vintage_missing_day_count} 天")
        if metric.baseline_missing_day_count:
            missing_notes.append(
                f"{label}基準線缺值合計 {metric.baseline_missing_day_count} 天"
                f"（涉及年份：{list(metric.baseline_years_with_missing_data)}）"
            )
    if missing_notes:
        lines.append("註：" + "；".join(missing_notes) + "，以上計算已排除缺值天數。")
    return "\n".join(lines)


# --- T-16 後續修正：氣候方向分類與確定性查詢字串 ---------------------------------


def classify_gdd_direction(anomaly: ClimateAnomaly) -> str | None:
    """依 GDD 距平判定該年份偏暖或偏涼，回傳知識庫 frontmatter 使用的英文詞彙。

    方向由程式從距平數字算出來，不交給 LLM 判斷——這是 T-16 驗證發現的核心問題：
    2013 Bordeaux（GDD −3.6%）與 2011 Napa（−11.2%）兩個偏涼年份都被檢索到偏暖規則，
    因為當時方向完全靠語意相似度決定。回傳值刻意對齊 `climate_rules/` 的
    `condition.temperature` 詞彙（`warmer`／`cooler`），可直接餵給 Chroma 的 `where`。

    Args:
        anomaly: 完整的距平結果。

    Returns:
        `"warmer"`、`"cooler"`，或方向不明時回傳 `None`（距平落在 deadband 內，或
        `pct_anomaly` 因基準值為 0 而無法計算）。`None` 表示不要施加方向過濾，讓檢索
        退回純語意相似度，而不是硬猜一個方向。
    """
    pct = anomaly.gdd.pct_anomaly
    if pct is None or abs(pct) < GDD_DIRECTION_DEADBAND_PCT:
        return None
    return "warmer" if pct > 0 else "cooler"


def _describe_precipitation_direction(anomaly: ClimateAnomaly) -> str:
    """把生長季降雨距平轉成查詢字串用的方向詞。"""
    pct = anomaly.season_precipitation.pct_anomaly
    if pct is None or abs(pct) < PRECIP_DIRECTION_DEADBAND_PCT:
        return "降雨接近平均"
    return "降雨偏多" if pct > 0 else "降雨偏少"


def format_direction_query(anomaly: ClimateAnomaly) -> str:
    """組出帶明確方向詞的檢索查詢句，取代原本直接拿 `format_anomaly_summary()` 當查詢。

    刻意不沿用 `_describe_pct_anomaly()` 的「高」／「少」措辭：那兩個字被產區名、年份、
    百分比數字稀釋後，在向量空間裡幾乎帶不動方向訊號（T-16 根因診斷）。這裡改用知識庫
    規則本身使用的「偏暖」「偏涼」「降雨偏多」等措辭，讓查詢向量跟目標規則的用字對齊。

    溫度方向另外會透過 metadata `where` 硬過濾，這個字串主要負責降雨軸的軟訊號——降雨
    刻意不硬篩，因為 10 則規則在「溫度 × 降雨」網格上有缺口（沒有 warm_normal），硬篩
    會讓降雨接近平均的年份（如 2003 Bordeaux）幾乎沒有規則存活。

    Args:
        anomaly: 完整的距平結果。

    Returns:
        例如「生長季偏涼、降雨偏多的年份，對葡萄成熟度與風味的影響」。
    """
    direction = classify_gdd_direction(anomaly)
    temperature_phrase = {"warmer": "偏暖", "cooler": "偏涼"}.get(direction, "溫度接近平均")
    return (
        f"生長季{temperature_phrase}、{_describe_precipitation_direction(anomaly)}的年份，"
        "對葡萄成熟度與風味的影響"
    )


def build_anomaly_payload(anomaly: ClimateAnomaly) -> dict[str, Any]:
    """把距平結果轉成下游共用的 dict，附上摘要、溫度方向與確定性查詢字串。

    `src/tools.py` 的工具 dispatch 與 `src/report.py` 的獨立 CLI 原本各自組一次
    `to_dict()` + `summary`，兩邊很容易改一處漏改另一處。方向欄位加進來之後這個風險更
    明顯，因此收斂成同一個函式。

    Args:
        anomaly: 完整的距平結果。

    Returns:
        距平 dict，額外含 `summary`、`temperature_direction`、`direction_query` 三個欄位。
        `temperature_direction` 為 `None` 時表示方向不明，下游不應施加方向過濾。
    """
    payload = anomaly.to_dict()
    payload["summary"] = format_anomaly_summary(anomaly)
    payload["temperature_direction"] = classify_gdd_direction(anomaly)
    payload["direction_query"] = format_direction_query(anomaly)
    return payload


# --- 逐月比較（T-15 圖表） ----------------------------------------------------


def _month_sequence(hemisphere: str) -> list[int]:
    """依生長季常數推導月份序列，順序是生長季順序，不是日曆 1–12 月。

    直接讀 `NORTHERN_SEASON`／`SOUTHERN_SEASON`（`growing_season_range()` 本身也是讀
    這兩個常數），不用假造一個參考年份去呼叫 `growing_season_range()` 再取 `.month`。
    南半球會跨年（例如 10、11、12、1、2、3、4），靠 `% 12` 處理年底到年初的wrap。

    Args:
        hemisphere: 半球代號，`"N"` 為北半球、`"S"` 為南半球。

    Returns:
        依生長季順序排列的月份數字列表（1–12）。
    """
    (start_month, _), (end_month, _) = (
        NORTHERN_SEASON if hemisphere.strip().upper() == "N" else SOUTHERN_SEASON
    )
    months: list[int] = []
    month = start_month
    while True:
        months.append(month)
        if month == end_month:
            return months
        month = month % 12 + 1


def _season_monthly_stats(season: SeasonClimate, *, by_calendar_month: bool = True) -> pd.DataFrame:
    """把單一生長季的每日資料依月份分組聚合。

    Args:
        season: 要聚合的生長季氣候資料。
        by_calendar_month: `True` 時依月份數字（1–12）分組，供跨年份平均使用；`False`
            時依「YYYY-MM」年月字串分組，保留年份供 `_monthly_table()` 人工除錯。

    Returns:
        以分組鍵為 index，欄位為 `temp_mean`（月均溫）、`temp_max`（月內最高溫）、
        `precipitation_mm`（月總降雨）的 DataFrame。
    """
    frame = season.to_dataframe()
    key = frame["date"].dt.month if by_calendar_month else frame["date"].dt.strftime("%Y-%m")
    return frame.groupby(key).agg(
        temp_mean=("temp_mean", "mean"),
        temp_max=("temp_max", "max"),
        precipitation_mm=("precipitation_mm", "sum"),
    )


def _baseline_monthly_stats(baseline: ClimateBaseline) -> pd.DataFrame:
    """把 30 年基準線依calendar月份聚合，跨年份平均。

    先用 `_season_monthly_stats()` 算出每一季各自的月均溫／月總降雨，再對這些「每季
    月統計」取平均——不能直接對攤平的每日資料做「日均降雨」再乘天數，那只有在每個月
    天數剛好一致時才會巧合對上（現有的生長季常數剛好都不含 2 月，但這是巧合，不該依賴）。

    Args:
        baseline: 30 年基準線氣候資料。

    Returns:
        以月份數字（1–12）為 index，欄位為 `temp_mean`（該月均溫的 30 年平均）、
        `precipitation_mm`（該月總降雨的 30 年平均）的 DataFrame。
    """
    per_season = pd.concat(_season_monthly_stats(season) for season in baseline.seasons)
    return per_season.groupby(per_season.index).agg(
        temp_mean=("temp_mean", "mean"),
        precipitation_mm=("precipitation_mm", "mean"),
    )


def build_monthly_comparison(season: SeasonClimate, baseline: ClimateBaseline) -> pd.DataFrame:
    """把單一年份與 30 年基準線的每月氣候攤成同一張表，供 T-15 圖表使用。

    依生長季順序排列（非日曆 1–12 月）——南半球產區（如 Mendoza、Marlborough）的生長季
    跨年，若照日曆順序排會從中間斷開，`_month_sequence()` 已處理這個 wrap-around。

    Args:
        season: 目標年份的生長季氣候資料。
        baseline: 30 年基準線氣候資料，須與 `season` 是同一產區。

    Returns:
        每列一個月，依生長季順序排列，欄位為 `month`（月份數字）、`month_label`
        （例："4月"）、`temp_mean_vintage`／`temp_mean_baseline`（月均溫，該年 vs
        30 年平均）、`precipitation_mm_vintage`／`precipitation_mm_baseline`（月總
        降雨，該年 vs 30 年平均）。
    """
    months = _month_sequence(season.hemisphere)
    vintage = _season_monthly_stats(season).reindex(months)
    base = _baseline_monthly_stats(baseline).reindex(months)
    return pd.DataFrame({
        "month": months,
        "month_label": [f"{m}月" for m in months],
        "temp_mean_vintage": vintage["temp_mean"].to_numpy(),
        "temp_mean_baseline": base["temp_mean"].to_numpy(),
        "precipitation_mm_vintage": vintage["precipitation_mm"].to_numpy(),
        "precipitation_mm_baseline": base["precipitation_mm"].to_numpy(),
    })


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
    monthly = _season_monthly_stats(season, by_calendar_month=False).round(1)
    monthly.index.name = "月份"
    monthly = monthly.rename(
        columns={"temp_mean": "平均溫", "temp_max": "最高溫", "precipitation_mm": "降雨mm"}
    )
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


def _run_anomaly_command(args: argparse.Namespace) -> None:
    """CLI：計算並印出單一年份 vs. 30 年基準線的距平摘要（T-07）。"""
    season = fetch_season_climate(args.region, args.year, use_cache=not args.refresh)
    baseline = get_baseline(args.region, refresh=args.refresh)
    anomaly = compute_climate_anomaly(season, baseline)
    print(format_anomaly_summary(anomaly))


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
    parser.add_argument("--anomaly", action="store_true",
                        help="計算指定年份 vs. 30 年基準線的 GDD／降雨距平，需搭配 --region 與 --year")
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
        elif args.anomaly:
            if not (args.region and args.year):
                parser.error("--anomaly 需要搭配 --region 與 --year")
            _run_anomaly_command(args)
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
