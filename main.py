"""Synchronize Steam owned game playtime data to Notion data sources."""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from typing import Optional

import requests


NOTION_VERSION_DEFAULT = "2025-09-03"
REQUEST_TIMEOUT_SECONDS = 30
MAX_RETRY_COUNT = 3
RETRY_STATUS_CODE_SET = {429, 500, 502, 503, 504}
STATE_FILE_ENV_NAME = "STEAM_SYNC_STATE_FILE"
ACHIEVEMENT_REQUEST_INTERVAL_ENV_NAME = "STEAM_ACHIEVEMENT_REQUEST_INTERVAL_SECONDS"
ACHIEVEMENT_MAX_WORKER_ENV_NAME = "STEAM_ACHIEVEMENT_MAX_WORKERS"
DEFAULT_ACHIEVEMENT_REQUEST_INTERVAL_SECONDS = 0.2
DEFAULT_ACHIEVEMENT_MAX_WORKER_COUNT = 5
NEW_GAME_UNRECORDED_PLAYTIME_THRESHOLD_MINUTES = 8 * 60
GAME_LOG_RELATION_PROPERTY_NAME = "GameLogRelation"
PERIOD_RELATION_PROPERTY_NAME = "PeriodRelation"
SUMMARY_RELATION_PROPERTY_NAME = "SummaryRelation"
MONTH_SUMMARY_RELATION_PROPERTY_NAME = "MonthSummaryRelation"
YEAR_SUMMARY_RELATION_PROPERTY_NAME = "YearSummaryRelation"
PLAYED_YEAR_PROPERTY_NAME = "PlayedYear"
PLAYED_MONTH_PROPERTY_NAME = "PlayedMonth"
RECENT_PLAYED_PROPERTY_NAME = "RecentPlayed"
BUY_YEAR_PROPERTY_NAME = "BuyYear"
BUY_MONTH_PROPERTY_NAME = "BuyMonth"
COMPLETE_YEAR_PROPERTY_NAME = "CompleteYear"
COMPLETE_MONTH_PROPERTY_NAME = "CompleteMonth"
FULL_ACHIEVEMENT_YEAR_PROPERTY_NAME = "FullAchievementYear"
FULL_ACHIEVEMENT_MONTH_PROPERTY_NAME = "FullAchievementMonth"
NEW_GAME_NUM_PROPERTY_NAME = "NewGameNum"
COMPLETE_GAME_NUM_PROPERTY_NAME = "CompleteGameNum"
FULL_ACHIEVEMENT_GAME_NUM_PROPERTY_NAME = "FullAchievementGameNum"
PLAYED_GAME_NUM_PROPERTY_NAME = "PlayedGameNum"
TOTAL_PLAYTIME_MINUTES_PROPERTY_NAME = "TotalPlayTimeMinutes"
PERIOD_TYPE_YEAR = "Year"
PERIOD_TYPE_MONTH = "Month"


@dataclass
class Config:
    """Runtime configuration loaded from environment variables."""

    steam_api_key: str
    steam_id64: str
    notion_api_key: str
    notion_game_data_source_id: str
    notion_playtime_data_source_id: str
    notion_period_data_source_id: str
    notion_summary_data_source_id: str
    notion_version: str


@dataclass
class RequestResult:
    """HTTP request result used to avoid raising exceptions across sync steps."""

    ok: bool
    data: dict[str, Any]
    status_code: Optional[int]
    error_message: str


@dataclass
class AchievementInfo:
    """Steam achievement summary for one game."""

    total_achievement: int
    achieved_achievement: int


@dataclass
class SteamGame:
    """Steam game snapshot returned by the Steam Web API."""

    app_id: int
    name: str
    total_playtime_minutes: int
    recent_played: int
    store_url: str
    header_image_url: Optional[str]
    icon_image_url: Optional[str]
    achievement_info: Optional[AchievementInfo]


@dataclass
class PeriodStat:
    """Notion period statistic row data."""

    page_id: str
    playtime_minutes: int
    period_text: Optional[str]
    period_type: Optional[str]


@dataclass
class PeriodPayload:
    """Period statistic payload context."""

    period_id: str
    period_text: str
    period_type: str
    year: int
    month: Optional[int]
    name: str


@dataclass
class PeriodSyncResult:
    """Period statistic and summary sync result."""

    period_page_id_list: list[str]
    year_summary_page_id: Optional[str]
    month_summary_page_id: Optional[str]
    year_text: str
    month_text: str


@dataclass
class PeriodStatSyncResult:
    """One period statistic sync result."""

    period_page_id: Optional[str]
    summary_page_id: Optional[str]


@dataclass
class NotionGamePage:
    """Notion page snapshot for one game row."""

    page_id: str
    app_id: int
    name: str
    total_playtime_minutes: int
    store_url: Optional[str]
    total_achievement: Optional[int]
    achieved_achievement: Optional[int]
    header_image_url: Optional[str]
    icon_image_url: Optional[str]
    unrecord_playtime_minutes: Optional[int]
    recent_played: int
    month_summary_page_id_list: list[str]
    year_summary_page_id_list: list[str]
    played_year_name_list: list[str]
    played_month_name_list: list[str]
    buy_year: Optional[str]
    buy_month: Optional[str]
    complete_year: Optional[str]
    complete_month: Optional[str]
    full_achievement_year: Optional[str]
    full_achievement_month: Optional[str]


@dataclass
class SummaryCount:
    """Full recomputed summary count values for one period."""

    new_game_count: int = 0
    complete_game_count: int = 0
    full_achievement_game_count: int = 0
    played_game_count: int = 0
    total_playtime_minutes: int = 0


@dataclass
class SyncStats:
    """Counters printed at the end of the sync run."""

    created_game_count: int = 0
    updated_game_count: int = 0
    unchanged_game_count: int = 0
    created_playtime_record_count: int = 0
    skipped_extra_notion_game_count: int = 0
    error_count: int = 0


@dataclass
class SyncState:
    """State persisted between GitHub Actions runs."""

    last_update_date: Optional[str] = None
    has_known_last_update_date: bool = False


class RequestRateLimiter:
    """Thread-safe limiter that spaces out request start times."""

    def __init__(self, interval_seconds: float) -> None:
        """Initialize the limiter with a minimum interval between request starts."""

        self.interval_seconds = max(0.0, interval_seconds)
        self.next_request_time = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        """Wait until the next request start slot is available."""

        if self.interval_seconds <= 0:
            return

        with self.lock:
            current_time = time.monotonic()
            scheduled_time = max(current_time, self.next_request_time)
            self.next_request_time = scheduled_time + self.interval_seconds

        wait_seconds = scheduled_time - current_time
        if wait_seconds > 0:
            time.sleep(wait_seconds)


def log_info(message: str) -> None:
    """Print an informational log line."""

    print(f"[INFO] {message}")


def log_warning(message: str) -> None:
    """Print a warning log line."""

    print(f"[WARN] {message}")


def log_error(message: str) -> None:
    """Print an error log line without raising."""

    print(f"[ERROR] {message}")


def load_config() -> Optional[Config]:
    """Load required configuration from environment variables."""

    required_name_list = [
        "STEAM_API_KEY",
        "STEAM_ID64",
        "NOTION_API_KEY",
        "NOTION_GAME_DATA_SOURCE_ID",
        "NOTION_PLAYTIME_DATA_SOURCE_ID",
    ]
    missing_name_list = [name for name in required_name_list if not os.environ.get(name)]

    if missing_name_list:
        log_error(
            "Missing required environment variable(s): "
            + ", ".join(missing_name_list)
            + ". Add them as GitHub Actions Secrets and pass them through workflow env."
        )
        return None

    return Config(
        steam_api_key=os.environ["STEAM_API_KEY"],
        steam_id64=os.environ["STEAM_ID64"],
        notion_api_key=os.environ["NOTION_API_KEY"],
        notion_game_data_source_id=os.environ["NOTION_GAME_DATA_SOURCE_ID"],
        notion_playtime_data_source_id=os.environ["NOTION_PLAYTIME_DATA_SOURCE_ID"],
        notion_period_data_source_id=os.environ.get("NOTION_PERIOD_DATA_SOURCE_ID", ""),
        notion_summary_data_source_id=os.environ.get("NOTION_SUMMARY_DATA_SOURCE_ID", ""),
        notion_version=os.environ.get("NOTION_VERSION", NOTION_VERSION_DEFAULT),
    )


def get_state_file_path() -> Path:
    """Return the state file path used to persist last update date."""

    configured_path = os.environ.get(STATE_FILE_ENV_NAME)
    if configured_path:
        return Path(configured_path)

    return Path(__file__).resolve().parent / ".steam_sync_state" / "state.json"


def load_sync_state(state_file_path: Path) -> SyncState:
    """Load sync state from a JSON file."""

    if not state_file_path.exists():
        log_info(f"No existing sync state file found. Treating this run as first initialization. path={state_file_path}")
        return SyncState()

    try:
        with state_file_path.open("r", encoding="utf-8") as state_file:
            state_data = json.load(state_file)
    except (OSError, json.JSONDecodeError) as error:
        log_warning(
            "Failed to read sync state file. "
            f"path={state_file_path}, reason={error}. Treating this run as first initialization."
        )
        return SyncState()

    if not isinstance(state_data, dict):
        log_warning(
            "Sync state file does not contain a JSON object. "
            f"path={state_file_path}. Treating this run as first initialization."
        )
        return SyncState()

    last_update_date = state_data.get("last_update_date")
    if isinstance(last_update_date, str):
        last_update_date_text = last_update_date.strip()
        if last_update_date_text:
            try:
                date.fromisoformat(last_update_date_text)
            except ValueError:
                log_warning(
                    "Sync state file contains an invalid last_update_date. "
                    f"path={state_file_path}, last_update_date={last_update_date_text}. "
                    "Treating this run as first initialization."
                )
                return SyncState()

            return SyncState(last_update_date=last_update_date_text, has_known_last_update_date=True)

    log_warning(
        "Sync state file does not contain a valid last_update_date. "
        f"path={state_file_path}. Treating this run as first initialization."
    )

    return SyncState()


def save_sync_state(state_file_path: Path, sync_state: SyncState) -> None:
    """Save sync state to a JSON file without exposing secrets."""

    try:
        state_file_path.parent.mkdir(parents=True, exist_ok=True)
        state_data = {"last_update_date": sync_state.last_update_date}
        with state_file_path.open("w", encoding="utf-8") as state_file:
            json.dump(state_data, state_file, ensure_ascii=False, indent=2)
            state_file.write("\n")
    except OSError as error:
        log_warning(f"Failed to save sync state file. path={state_file_path}, reason={error}")


def send_http_request(
    method: str,
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    json_body: Optional[dict[str, Any]] = None,
    query_param: Optional[dict[str, Any]] = None,
) -> RequestResult:
    """Send an HTTP request with small retry handling and no uncaught exception."""

    for attempt_index in range(1, MAX_RETRY_COUNT + 1):
        try:
            response = requests.request(
                method=method,
                url=url,
                headers=headers,
                json=json_body,
                params=query_param,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as error:
            if attempt_index < MAX_RETRY_COUNT:
                time.sleep(attempt_index)
                continue
            return RequestResult(False, {}, None, str(error))

        if response.status_code in RETRY_STATUS_CODE_SET and attempt_index < MAX_RETRY_COUNT:
            retry_after_text = response.headers.get("Retry-After")
            retry_after_seconds = parse_retry_after(retry_after_text, attempt_index)
            time.sleep(retry_after_seconds)
            continue

        if response.status_code < 200 or response.status_code >= 300:
            return RequestResult(False, {}, response.status_code, response.text)

        try:
            return RequestResult(True, response.json(), response.status_code, "")
        except ValueError as error:
            return RequestResult(False, {}, response.status_code, f"Invalid JSON response: {error}")

    return RequestResult(False, {}, None, "Request retry loop ended unexpectedly.")


def parse_retry_after(retry_after_text: Optional[str], fallback_seconds: int) -> int:
    """Parse a Retry-After header value."""

    if retry_after_text is None:
        return fallback_seconds

    try:
        retry_after_seconds = int(retry_after_text)
    except ValueError:
        return fallback_seconds

    return max(1, retry_after_seconds)


def get_achievement_request_interval_seconds() -> float:
    """Read the achievement request interval from environment variables."""

    interval_text = os.environ.get(ACHIEVEMENT_REQUEST_INTERVAL_ENV_NAME)
    if not interval_text:
        return DEFAULT_ACHIEVEMENT_REQUEST_INTERVAL_SECONDS

    try:
        interval_seconds = float(interval_text)
    except ValueError:
        log_warning(
            f"Invalid {ACHIEVEMENT_REQUEST_INTERVAL_ENV_NAME}={interval_text}. "
            f"Using default {DEFAULT_ACHIEVEMENT_REQUEST_INTERVAL_SECONDS} seconds."
        )
        return DEFAULT_ACHIEVEMENT_REQUEST_INTERVAL_SECONDS

    if interval_seconds < 0:
        log_warning(
            f"Invalid {ACHIEVEMENT_REQUEST_INTERVAL_ENV_NAME}={interval_text}. "
            f"Using default {DEFAULT_ACHIEVEMENT_REQUEST_INTERVAL_SECONDS} seconds."
        )
        return DEFAULT_ACHIEVEMENT_REQUEST_INTERVAL_SECONDS

    return interval_seconds


def get_achievement_max_worker_count() -> int:
    """Read the achievement request worker count from environment variables."""

    worker_text = os.environ.get(ACHIEVEMENT_MAX_WORKER_ENV_NAME)
    if not worker_text:
        return DEFAULT_ACHIEVEMENT_MAX_WORKER_COUNT

    try:
        worker_count = int(worker_text)
    except ValueError:
        log_warning(
            f"Invalid {ACHIEVEMENT_MAX_WORKER_ENV_NAME}={worker_text}. "
            f"Using default {DEFAULT_ACHIEVEMENT_MAX_WORKER_COUNT}."
        )
        return DEFAULT_ACHIEVEMENT_MAX_WORKER_COUNT

    if worker_count <= 0:
        log_warning(
            f"Invalid {ACHIEVEMENT_MAX_WORKER_ENV_NAME}={worker_text}. "
            f"Using default {DEFAULT_ACHIEVEMENT_MAX_WORKER_COUNT}."
        )
        return DEFAULT_ACHIEVEMENT_MAX_WORKER_COUNT

    return worker_count


def notion_headers(config: Config) -> dict[str, str]:
    """Build Notion request headers."""

    return {
        "Authorization": f"Bearer {config.notion_api_key}",
        "Content-Type": "application/json",
        "Notion-Version": config.notion_version,
    }


def notion_request(
    config: Config,
    method: str,
    url: str,
    *,
    json_body: Optional[dict[str, Any]] = None,
) -> RequestResult:
    """Send a Notion API request."""

    return send_http_request(method, url, headers=notion_headers(config), json_body=json_body)


def build_icon_image_url(app_id: int, image_icon_hash: Optional[str]) -> Optional[str]:
    """Build the Steam icon image URL for a game."""

    if not image_icon_hash:
        return None

    return f"http://media.steampowered.com/steamcommunity/public/images/apps/{app_id}/{image_icon_hash}.jpg"


def build_fallback_header_image_url(app_id: int) -> str:
    """Build the fallback Steam CDN header image URL for a game."""

    return f"https://cdn.cloudflare.steamstatic.com/steam/apps/{app_id}/header.jpg"


def ensure_header_image_url(steam_game: SteamGame, current_header_image_url: Optional[str]) -> None:
    """Fetch a Steam header image URL only when Notion does not already have one."""

    if current_header_image_url:
        return

    steam_game.header_image_url = fetch_header_image_url_from_steam_store(steam_game)


def fetch_header_image_url_from_steam_store(steam_game: SteamGame) -> Optional[str]:
    """Fetch the Steam store header image URL for one game."""

    url = "https://store.steampowered.com/api/appdetails"
    result = send_http_request("GET", url, query_param={"appids": steam_game.app_id, "cc": "us"})

    if not result.ok:
        log_warning(
            "Failed to fetch Steam store header image. "
            f"{build_game_context(steam_game)}, status={result.status_code}, reason={result.error_message}"
        )
        return fetch_fallback_header_image_url(steam_game)

    app_data = result.data.get(str(steam_game.app_id))
    if not isinstance(app_data, dict):
        log_warning(f"Steam store response has no app data. {build_game_context(steam_game)}")
        return fetch_fallback_header_image_url(steam_game)

    data = app_data.get("data")
    if not isinstance(data, dict):
        log_warning(f"Steam store response has no data object. {build_game_context(steam_game)}")
        return fetch_fallback_header_image_url(steam_game)

    header_image_url = data.get("header_image")
    if not header_image_url:
        log_warning(f"Steam store response has no header_image field. {build_game_context(steam_game)}")
        return fetch_fallback_header_image_url(steam_game)

    return str(header_image_url)


def fetch_fallback_header_image_url(steam_game: SteamGame) -> Optional[str]:
    """Return fallback header image URL only when it responds as an image."""

    fallback_url = build_fallback_header_image_url(steam_game.app_id)
    if is_image_url_available(fallback_url):
        return fallback_url

    log_warning(f"Fallback Steam header image URL is not available. {build_game_context(steam_game)}")
    return None


def is_image_url_available(image_url: str) -> bool:
    """Check whether a URL responds successfully with an image content type."""

    result = send_image_probe_request("HEAD", image_url)
    if result is None:
        result = send_image_probe_request("GET", image_url)

    return result is True


def send_image_probe_request(method: str, image_url: str) -> Optional[bool]:
    """Send one image probe request and return None when another method should be tried."""

    try:
        response = requests.request(method=method, url=image_url, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException:
        return False

    if response.status_code == 405 and method == "HEAD":
        return None

    if response.status_code < 200 or response.status_code >= 300:
        return False

    content_type = response.headers.get("Content-Type", "")
    return content_type.lower().startswith("image/")


def fetch_steam_game_list(config: Config) -> Optional[list[SteamGame]]:
    """Fetch and merge owned games and recently played games."""

    owned_game_list = fetch_owned_steam_game_list(config)
    recent_game_list = fetch_recent_steam_game_list(config)

    if owned_game_list is None and recent_game_list is None:
        log_error(
            "Failed to fetch Steam games from both GetOwnedGames and GetRecentlyPlayedGames. "
            "Check STEAM_API_KEY, STEAM_ID64, network access, and Steam profile privacy."
        )
        return None

    if owned_game_list is None:
        log_warning("GetOwnedGames failed. Continuing with GetRecentlyPlayedGames result only.")
        owned_game_list = []

    if recent_game_list is None:
        log_warning("GetRecentlyPlayedGames failed. Continuing with GetOwnedGames result only.")
        recent_game_list = []

    merged_game_list, recent_added_count = merge_steam_game_list(owned_game_list, recent_game_list)
    log_info(
        "Fetched Steam games. "
        f"owned_count={len(owned_game_list)}, "
        f"recent_count={len(recent_game_list)}, "
        f"recent_added_count={recent_added_count}, "
        f"merged_count={len(merged_game_list)}"
    )
    return merged_game_list


def fetch_owned_steam_game_list(config: Config) -> Optional[list[SteamGame]]:
    """Fetch owned games for the configured Steam user."""

    url = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
    query_param = {
        "key": config.steam_api_key,
        "steamid": config.steam_id64,
        "include_appinfo": "true",
        "include_played_free_games": "true",
        "format": "json",
    }
    result = send_http_request("GET", url, query_param=query_param)

    if not result.ok:
        log_error(
            "Failed to fetch Steam owned games. "
            f"status={result.status_code}, reason={result.error_message}. "
            "Check STEAM_API_KEY, STEAM_ID64, network access, and Steam profile privacy."
        )
        return None

    raw_game_list = result.data.get("response", {}).get("games", [])
    return parse_steam_game_list(raw_game_list, "GetOwnedGames", 0)


def fetch_recent_steam_game_list(config: Config) -> Optional[list[SteamGame]]:
    """Fetch recently played games for the configured Steam user."""

    url = "https://api.steampowered.com/IPlayerService/GetRecentlyPlayedGames/v1/"
    query_param = {
        "key": config.steam_api_key,
        "steamid": config.steam_id64,
        "format": "json",
    }
    result = send_http_request("GET", url, query_param=query_param)

    if not result.ok:
        log_error(
            "Failed to fetch Steam recently played games. "
            f"status={result.status_code}, reason={result.error_message}. "
            "Check STEAM_API_KEY, STEAM_ID64, network access, and Steam profile privacy."
        )
        return None

    raw_game_list = result.data.get("response", {}).get("games", [])
    return parse_steam_game_list(raw_game_list, "GetRecentlyPlayedGames", 1)


def parse_steam_game_list(raw_game_list: Any, source_name: str, recent_played: int) -> list[SteamGame]:
    """Parse raw Steam game objects from one Steam API response."""

    steam_game_list: list[SteamGame] = []
    if not isinstance(raw_game_list, list):
        log_warning(f"{source_name} returned invalid games field. Treating it as empty.")
        return steam_game_list

    for raw_game in raw_game_list:
        if not isinstance(raw_game, dict):
            log_warning(f"Skipped malformed Steam game entry from {source_name}: {raw_game}")
            continue

        steam_game = parse_steam_game(raw_game, recent_played)
        if steam_game is None:
            log_warning(f"Skipped malformed Steam game entry from {source_name}: {raw_game}")
            continue
        steam_game_list.append(steam_game)

    return steam_game_list


def merge_steam_game_list(
    owned_game_list: list[SteamGame],
    recent_game_list: list[SteamGame],
) -> tuple[list[SteamGame], int]:
    """Merge owned and recent Steam games by AppID, preserving owned data first."""

    merged_game_list: list[SteamGame] = []
    app_id_to_steam_game: dict[int, SteamGame] = {}

    for steam_game in owned_game_list:
        if steam_game.app_id in app_id_to_steam_game:
            continue
        merged_game_list.append(steam_game)
        app_id_to_steam_game[steam_game.app_id] = steam_game

    recent_added_count = 0
    for steam_game in recent_game_list:
        existing_steam_game = app_id_to_steam_game.get(steam_game.app_id)
        if existing_steam_game is not None:
            existing_steam_game.recent_played = 1
            continue
        merged_game_list.append(steam_game)
        app_id_to_steam_game[steam_game.app_id] = steam_game
        recent_added_count += 1

    return merged_game_list, recent_added_count


def parse_steam_game(raw_game: dict[str, Any], recent_played: int) -> Optional[SteamGame]:
    """Convert one Steam API game object to a SteamGame."""

    app_id = safe_int(raw_game.get("appid"))
    if app_id is None:
        return None

    total_playtime_minutes = safe_int(raw_game.get("playtime_forever")) or 0
    raw_name = raw_game.get("name")
    name = str(raw_name).strip() if raw_name else f"Unknown Game {app_id}"
    raw_icon_hash = raw_game.get("img_icon_url")
    image_icon_hash = str(raw_icon_hash).strip() if raw_icon_hash else None

    return SteamGame(
        app_id=app_id,
        name=name,
        total_playtime_minutes=total_playtime_minutes,
        recent_played=1 if recent_played else 0,
        store_url=f"https://store.steampowered.com/app/{app_id}/",
        header_image_url=None,
        icon_image_url=build_icon_image_url(app_id, image_icon_hash),
        achievement_info=None,
    )


def update_game_achievement_info_list(config: Config, steam_game_list: list[SteamGame]) -> None:
    """Fetch and attach achievement info for every Steam game."""

    interval_seconds = get_achievement_request_interval_seconds()
    max_worker_count = get_achievement_max_worker_count()
    total_game_count = len(steam_game_list)
    if total_game_count == 0:
        log_info("No Steam games found. Achievement sync is skipped.")
        return

    worker_count = min(max_worker_count, total_game_count)
    log_info(
        "Fetching Steam achievements. "
        f"game_count={total_game_count}, "
        f"worker_count={worker_count}, "
        f"interval_seconds={interval_seconds}"
    )

    rate_limiter = RequestRateLimiter(interval_seconds)
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_to_steam_game = {
            executor.submit(
                fetch_game_achievement_info_with_rate_limit,
                config,
                steam_game,
                rate_limiter,
            ): steam_game
            for steam_game in steam_game_list
        }

        completed_count = 0
        for future in as_completed(future_to_steam_game):
            completed_count += 1
            steam_game = future_to_steam_game[future]

            try:
                achievement_info = future.result()
            except Exception as error:
                log_warning(f"Unexpected achievement worker error. {build_game_context(steam_game)}, reason={error}")
                continue

            steam_game.achievement_info = achievement_info
            if steam_game.achievement_info is not None:
                log_info(
                    "Fetched achievement info. "
                    f"{build_game_context(steam_game)}, "
                    f"achieved={steam_game.achievement_info.achieved_achievement}, "
                    f"total={steam_game.achievement_info.total_achievement}, "
                    f"progress={completed_count}/{total_game_count}"
                )


def fetch_game_achievement_info_with_rate_limit(
    config: Config,
    steam_game: SteamGame,
    rate_limiter: RequestRateLimiter,
) -> Optional[AchievementInfo]:
    """Fetch achievement info after waiting for the shared request rate limiter."""

    rate_limiter.wait()
    return fetch_game_achievement_info(config, steam_game)


def fetch_game_achievement_info(config: Config, steam_game: SteamGame) -> Optional[AchievementInfo]:
    """Fetch achievement summary for one Steam game."""

    url = "https://api.steampowered.com/ISteamUserStats/GetPlayerAchievements/v1/"
    query_param = {
        "key": config.steam_api_key,
        "steamid": config.steam_id64,
        "appid": steam_game.app_id,
        "format": "json",
    }
    result = send_http_request("GET", url, query_param=query_param)

    if not result.ok:
        log_warning(
            "Failed to fetch Steam achievements. "
            f"{build_game_context(steam_game)}, status={result.status_code}, reason={result.error_message}"
        )
        return None

    player_stats = result.data.get("playerstats")
    if not isinstance(player_stats, dict):
        log_warning(f"Steam achievement response has no playerstats object. {build_game_context(steam_game)}")
        return None

    if player_stats.get("success") is False:
        log_warning(
            "Steam achievement response reported success=false. "
            f"{build_game_context(steam_game)}, reason={player_stats.get('error')}"
        )
        return None

    achievement_list = player_stats.get("achievements")
    if achievement_list is None:
        return AchievementInfo(total_achievement=0, achieved_achievement=0)

    if not isinstance(achievement_list, list):
        log_warning(f"Steam achievement response has invalid achievements field. {build_game_context(steam_game)}")
        return None

    achieved_achievement = 0
    for achievement in achievement_list:
        if not isinstance(achievement, dict):
            continue
        achieved_value = achievement.get("achieved")
        if achieved_value is True or achieved_value == 1:
            achieved_achievement += 1

    return AchievementInfo(
        total_achievement=len(achievement_list),
        achieved_achievement=achieved_achievement,
    )


def safe_int(value: Any) -> Optional[int]:
    """Convert a value to int when possible."""

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def query_all_notion_pages(config: Config, data_source_id: str) -> Optional[list[dict[str, Any]]]:
    """Query every page in one Notion data source."""

    data_source_url = f"https://api.notion.com/v1/data_sources/{data_source_id}/query"
    database_url = f"https://api.notion.com/v1/databases/{data_source_id}/query"

    page_list = query_all_notion_pages_from_url(config, data_source_url)
    if page_list is not None:
        return page_list

    log_warning(
        "Data source query failed. Retrying with legacy database query endpoint "
        "in case the provided id is a database id."
    )
    return query_all_notion_pages_from_url(config, database_url)


def query_all_notion_pages_from_url(config: Config, url: str) -> Optional[list[dict[str, Any]]]:
    """Query all pages from a Notion query endpoint URL."""

    page_list: list[dict[str, Any]] = []
    start_cursor: Optional[str] = None

    while True:
        body: dict[str, Any] = {"page_size": 100}
        if start_cursor is not None:
            body["start_cursor"] = start_cursor

        result = notion_request(config, "POST", url, json_body=body)
        if not result.ok:
            log_error(
                "Failed to query Notion pages. "
                f"url={url}, status={result.status_code}, reason={result.error_message}"
            )
            return None

        result_list = result.data.get("results", [])
        if isinstance(result_list, list):
            page_list.extend(result_list)

        if not result.data.get("has_more"):
            return page_list

        start_cursor = result.data.get("next_cursor")
        if not start_cursor:
            log_error("Notion query reported has_more=true but did not return next_cursor.")
            return None


def query_period_stat_page_list(config: Config, period_id: str) -> Optional[list[dict[str, Any]]]:
    """Query period statistic pages by PeriodID."""

    if not config.notion_period_data_source_id:
        log_error(
            "Missing NOTION_PERIOD_DATA_SOURCE_ID. "
            f"Period statistic sync is skipped for period_id={period_id}."
        )
        return None

    filter_body = {
        "filter": {
            "property": "PeriodID",
            "rich_text": {
                "equals": period_id,
            },
        }
    }
    data_source_url = f"https://api.notion.com/v1/data_sources/{config.notion_period_data_source_id}/query"
    database_url = f"https://api.notion.com/v1/databases/{config.notion_period_data_source_id}/query"

    page_list = query_notion_pages_from_url_with_body(config, data_source_url, filter_body)
    if page_list is not None:
        return page_list

    log_warning(
        "Period statistic data source query failed. Retrying with legacy database query endpoint "
        "in case the provided id is a database id."
    )
    return query_notion_pages_from_url_with_body(config, database_url, filter_body)


def query_summary_page_list(
    config: Config,
    period_text: str,
    period_type: str,
) -> Optional[list[dict[str, Any]]]:
    """Query summary pages by Period title and Type select."""

    if not config.notion_summary_data_source_id:
        log_error(
            "Missing NOTION_SUMMARY_DATA_SOURCE_ID. "
            f"Summary sync is skipped for period={period_text}, type={period_type}."
        )
        return None

    filter_body = {
        "filter": {
            "and": [
                {
                    "property": "Period",
                    "title": {
                        "equals": period_text,
                    },
                },
                {
                    "property": "Type",
                    "select": {
                        "equals": period_type,
                    },
                },
            ]
        }
    }
    data_source_url = f"https://api.notion.com/v1/data_sources/{config.notion_summary_data_source_id}/query"
    database_url = f"https://api.notion.com/v1/databases/{config.notion_summary_data_source_id}/query"

    page_list = query_notion_pages_from_url_with_body(config, data_source_url, filter_body)
    if page_list is not None:
        return page_list

    log_warning(
        "Summary data source query failed. Retrying with legacy database query endpoint "
        "in case the provided id is a database id."
    )
    return query_notion_pages_from_url_with_body(config, database_url, filter_body)


def query_notion_pages_from_url_with_body(
    config: Config,
    url: str,
    base_body: dict[str, Any],
) -> Optional[list[dict[str, Any]]]:
    """Query all pages from a Notion query endpoint with an extra body."""

    page_list: list[dict[str, Any]] = []
    start_cursor: Optional[str] = None

    while True:
        body = dict(base_body)
        body["page_size"] = 100
        if start_cursor is not None:
            body["start_cursor"] = start_cursor

        result = notion_request(config, "POST", url, json_body=body)
        if not result.ok:
            log_error(
                "Failed to query Notion pages. "
                f"url={url}, status={result.status_code}, reason={result.error_message}"
            )
            return None

        result_list = result.data.get("results", [])
        if isinstance(result_list, list):
            page_list.extend(result_list)

        if not result.data.get("has_more"):
            return page_list

        start_cursor = result.data.get("next_cursor")
        if not start_cursor:
            log_error("Notion query reported has_more=true but did not return next_cursor.")
            return None


def build_notion_game_page_index(page_list: list[dict[str, Any]], stats: SyncStats) -> dict[int, NotionGamePage]:
    """Build an AppID keyed index from Notion game pages."""

    page_index: dict[int, NotionGamePage] = {}

    for page in page_list:
        notion_game_page = parse_notion_game_page(page)
        if notion_game_page is None:
            page_id = str(page.get("id", "unknown"))
            log_warning(f"Skipped Notion game page without valid AppID. page_id={page_id}")
            continue

        if notion_game_page.app_id in page_index:
            stats.error_count += 1
            log_error(
                "Duplicate AppID found in Notion game table. "
                f"app_id={notion_game_page.app_id}, "
                f"kept_page_id={page_index[notion_game_page.app_id].page_id}, "
                f"ignored_page_id={notion_game_page.page_id}. "
                "Please merge duplicate rows manually."
            )
            continue

        page_index[notion_game_page.app_id] = notion_game_page

    return page_index


def sync_recent_played_for_all_notion_games(
    config: Config,
    notion_game_page_index: dict[int, NotionGamePage],
    steam_game_list: list[SteamGame],
) -> int:
    """Synchronize RecentPlayed for all Notion games to the current Steam recent set."""

    recent_app_id_set = {
        steam_game.app_id
        for steam_game in steam_game_list
        if steam_game.recent_played == 1
    }
    updated_count = 0
    skipped_count = 0
    error_count = 0

    for notion_game_page in sorted(notion_game_page_index.values(), key=lambda item: item.name.lower()):
        target_recent_played = 1 if notion_game_page.app_id in recent_app_id_set else 0
        if notion_game_page.recent_played == target_recent_played:
            skipped_count += 1
            continue

        updated = update_notion_page(
            config,
            notion_game_page.page_id,
            {RECENT_PLAYED_PROPERTY_NAME: {"number": target_recent_played}},
            (
                "recent_played_sync, "
                f"app_id={notion_game_page.app_id}, name={notion_game_page.name}, "
                f"target_recent_played={target_recent_played}"
            ),
        )
        if updated:
            notion_game_page.recent_played = target_recent_played
            updated_count += 1
            continue

        error_count += 1

    log_info(
        "Synced RecentPlayed for Notion game table. "
        f"recent_count={len(recent_app_id_set)}, "
        f"updated_count={updated_count}, "
        f"skipped_count={skipped_count}, "
        f"error_count={error_count}"
    )
    return error_count


def parse_notion_game_page(page: dict[str, Any]) -> Optional[NotionGamePage]:
    """Parse one Notion game page."""

    property_map = page.get("properties", {})
    app_id = get_notion_number(property_map, "AppID")
    if app_id is None:
        return None

    return NotionGamePage(
        page_id=str(page.get("id", "")),
        app_id=int(app_id),
        name=get_notion_title(property_map, "Name") or f"Unknown Game {int(app_id)}",
        total_playtime_minutes=int(get_notion_number(property_map, "TotalPlaytimeMinutes") or 0),
        store_url=get_notion_url(property_map, "StoreUrl"),
        total_achievement=get_optional_notion_int(property_map, "TotalAchievement"),
        achieved_achievement=get_optional_notion_int(property_map, "AchievedAchievement"),
        header_image_url=get_notion_url(property_map, "HeaderImageUrl"),
        icon_image_url=get_notion_url(property_map, "IconImageUrl"),
        unrecord_playtime_minutes=get_optional_notion_int(property_map, "UnrecordPlaytimeMinutes"),
        recent_played=int(get_notion_number(property_map, RECENT_PLAYED_PROPERTY_NAME) or 0),
        month_summary_page_id_list=get_notion_relation_id_list(property_map, MONTH_SUMMARY_RELATION_PROPERTY_NAME),
        year_summary_page_id_list=get_notion_relation_id_list(property_map, YEAR_SUMMARY_RELATION_PROPERTY_NAME),
        played_year_name_list=get_notion_multi_select_name_list(property_map, PLAYED_YEAR_PROPERTY_NAME),
        played_month_name_list=get_notion_multi_select_name_list(property_map, PLAYED_MONTH_PROPERTY_NAME),
        buy_year=get_notion_formula_year_text(property_map, BUY_YEAR_PROPERTY_NAME),
        buy_month=get_notion_formula_month_text(property_map, BUY_MONTH_PROPERTY_NAME),
        complete_year=get_notion_formula_year_text(property_map, COMPLETE_YEAR_PROPERTY_NAME),
        complete_month=get_notion_formula_month_text(property_map, COMPLETE_MONTH_PROPERTY_NAME),
        full_achievement_year=get_notion_formula_year_text(property_map, FULL_ACHIEVEMENT_YEAR_PROPERTY_NAME),
        full_achievement_month=get_notion_formula_month_text(property_map, FULL_ACHIEVEMENT_MONTH_PROPERTY_NAME),
    )


def get_notion_title(property_map: dict[str, Any], property_name: str) -> Optional[str]:
    """Read a Notion title property as plain text."""

    title_item_list = property_map.get(property_name, {}).get("title", [])
    text_part_list: list[str] = []

    for title_item in title_item_list:
        plain_text = title_item.get("plain_text")
        if plain_text:
            text_part_list.append(str(plain_text))

    title_text = "".join(text_part_list).strip()
    return title_text if title_text else None


def get_notion_rich_text(property_map: dict[str, Any], property_name: str) -> Optional[str]:
    """Read a Notion rich text property as plain text."""

    rich_text_item_list = property_map.get(property_name, {}).get("rich_text", [])
    if not isinstance(rich_text_item_list, list):
        return None

    text_part_list: list[str] = []
    for rich_text_item in rich_text_item_list:
        if not isinstance(rich_text_item, dict):
            continue
        plain_text = rich_text_item.get("plain_text")
        if plain_text:
            text_part_list.append(str(plain_text))
            continue

        text_object = rich_text_item.get("text")
        if isinstance(text_object, dict):
            content = text_object.get("content")
            if content:
                text_part_list.append(str(content))

    rich_text = "".join(text_part_list).strip()
    return rich_text if rich_text else None


def get_notion_formula_year_text(property_map: dict[str, Any], property_name: str) -> Optional[str]:
    """Read a Notion formula property and normalize it as YYYY."""

    formula_text = get_notion_formula_value_text(property_map, property_name)
    if formula_text is None:
        return None

    return normalize_formula_year_text(formula_text, property_name)


def get_notion_formula_month_text(property_map: dict[str, Any], property_name: str) -> Optional[str]:
    """Read a Notion formula property and normalize it as YYYY-MM."""

    formula_text = get_notion_formula_value_text(property_map, property_name)
    if formula_text is None:
        return None

    return normalize_formula_month_text(formula_text, property_name)


def get_notion_formula_value_text(property_map: dict[str, Any], property_name: str) -> Optional[str]:
    """Read a Notion formula result as text."""

    formula_value = property_map.get(property_name, {}).get("formula")
    if not isinstance(formula_value, dict):
        return None

    formula_type = formula_value.get("type")
    if formula_type == "number":
        number_value = formula_value.get("number")
        if isinstance(number_value, (int, float)):
            if float(number_value).is_integer():
                return str(int(number_value))
            return str(number_value).strip()
        return None

    if formula_type == "string":
        string_value = formula_value.get("string")
        if isinstance(string_value, str):
            string_value = string_value.strip()
            return string_value if string_value else None
        return None

    log_warning(
        "Unsupported Notion formula result type for summary field. "
        f"property_name={property_name}, formula_type={formula_type}"
    )
    return None


def normalize_formula_year_text(value_text: str, property_name: str) -> Optional[str]:
    """Normalize formula year value to YYYY."""

    value_text = value_text.strip()
    if is_valid_year_text(value_text):
        return value_text

    log_warning(
        "Skipped invalid formula year value. "
        f"property_name={property_name}, value={value_text}. Expected YYYY."
    )
    return None


def normalize_formula_month_text(value_text: str, property_name: str) -> Optional[str]:
    """Normalize formula month value to YYYY-MM."""

    value_text = value_text.strip()
    if is_valid_month_text(value_text):
        return value_text

    if len(value_text) == 6 and value_text.isdigit():
        normalized_text = f"{value_text[:4]}-{value_text[4:]}"
        if is_valid_month_text(normalized_text):
            return normalized_text

    log_warning(
        "Skipped invalid formula month value. "
        f"property_name={property_name}, value={value_text}. Expected YYYY-MM or YYYYMM."
    )
    return None


def get_notion_number(property_map: dict[str, Any], property_name: str) -> Optional[float]:
    """Read a Notion number property."""

    number_value = property_map.get(property_name, {}).get("number")
    if isinstance(number_value, (int, float)):
        return float(number_value)
    return None


def get_optional_notion_int(property_map: dict[str, Any], property_name: str) -> Optional[int]:
    """Read a Notion number property as an optional integer."""

    number_value = get_notion_number(property_map, property_name)
    if number_value is None:
        return None
    return int(number_value)


def get_notion_url(property_map: dict[str, Any], property_name: str) -> Optional[str]:
    """Read a Notion URL property."""

    url_value = property_map.get(property_name, {}).get("url")
    return str(url_value) if url_value else None


def get_notion_select_name(property_map: dict[str, Any], property_name: str) -> Optional[str]:
    """Read a Notion select property option name."""

    select_value = property_map.get(property_name, {}).get("select")
    if not isinstance(select_value, dict):
        return None

    select_name = select_value.get("name")
    if not isinstance(select_name, str):
        return None

    select_name = select_name.strip()
    return select_name if select_name else None


def get_notion_relation_id_list(property_map: dict[str, Any], property_name: str) -> list[str]:
    """Read Notion relation page ids."""

    relation_list = property_map.get(property_name, {}).get("relation", [])
    if not isinstance(relation_list, list):
        return []

    page_id_list: list[str] = []
    for relation_item in relation_list:
        if not isinstance(relation_item, dict):
            continue
        page_id = relation_item.get("id")
        if isinstance(page_id, str) and page_id:
            page_id_list.append(page_id)

    return page_id_list


def get_notion_multi_select_name_list(property_map: dict[str, Any], property_name: str) -> list[str]:
    """Read Notion multi-select option names."""

    multi_select_list = property_map.get(property_name, {}).get("multi_select", [])
    if not isinstance(multi_select_list, list):
        return []

    name_list: list[str] = []
    for multi_select_item in multi_select_list:
        if not isinstance(multi_select_item, dict):
            continue
        name = multi_select_item.get("name")
        if isinstance(name, str) and name:
            name_list.append(name)

    return name_list


def parse_period_stat_page(page: dict[str, Any]) -> Optional[PeriodStat]:
    """Parse one Notion period statistic page."""

    page_id = page.get("id")
    if not isinstance(page_id, str) or not page_id:
        log_warning("Skipped period statistic page without page id.")
        return None

    property_map = page.get("properties", {})
    if not isinstance(property_map, dict):
        log_warning(f"Skipped period statistic page without valid properties. page_id={page_id}")
        return None

    return PeriodStat(
        page_id=page_id,
        playtime_minutes=int(get_notion_number(property_map, "PlayTimeMinutes") or 0),
        period_text=get_notion_select_name(property_map, "Period"),
        period_type=get_notion_select_name(property_map, "Type"),
    )


def build_game_properties(
    steam_game: SteamGame,
    include_playtime: bool,
    unrecord_playtime_minutes: Optional[int] = None,
) -> dict[str, Any]:
    """Build Notion properties for a game total table row."""

    property_map = {
        "Name": build_title_property(steam_game.name),
        "AppID": {"number": steam_game.app_id},
        RECENT_PLAYED_PROPERTY_NAME: {"number": steam_game.recent_played},
        "StoreUrl": {"url": steam_game.store_url},
    }

    if steam_game.header_image_url:
        property_map["HeaderImageUrl"] = {"url": steam_game.header_image_url}

    if steam_game.icon_image_url:
        property_map["IconImageUrl"] = {"url": steam_game.icon_image_url}

    if include_playtime:
        property_map["TotalPlaytimeMinutes"] = {"number": steam_game.total_playtime_minutes}
        if steam_game.achievement_info is not None:
            property_map["TotalAchievement"] = {"number": steam_game.achievement_info.total_achievement}
            property_map["AchievedAchievement"] = {"number": steam_game.achievement_info.achieved_achievement}

    if unrecord_playtime_minutes is not None:
        property_map["UnrecordPlaytimeMinutes"] = {"number": unrecord_playtime_minutes}

    return property_map


def build_playtime_properties(
    steam_game: SteamGame,
    delta_minutes: int,
    record_date: str,
    game_page_id: Optional[str] = None,
    period_page_id_list: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Build Notion properties for one playtime history row."""

    property_map = {
        "Name": build_title_property(steam_game.name),
        "AppID": {"number": steam_game.app_id},
        "Date": build_date_property(record_date),
        "TotalPlaytimeMinutes": {"number": steam_game.total_playtime_minutes},
        "DeltaMinutes": {"number": delta_minutes},
    }

    if game_page_id:
        property_map[GAME_LOG_RELATION_PROPERTY_NAME] = build_relation_property(game_page_id)

    if period_page_id_list:
        property_map[PERIOD_RELATION_PROPERTY_NAME] = build_relation_list_property(period_page_id_list)

    return property_map


def build_relation_property(game_page_id: str) -> dict[str, Any]:
    """Build a Notion relation property payload for one related game page."""

    return build_relation_list_property([game_page_id])


def build_relation_list_property(page_id_list: list[str]) -> dict[str, Any]:
    """Build a Notion relation property payload for related pages."""

    return {
        "relation": [
            {"id": page_id}
            for page_id in page_id_list
        ]
    }


def merge_unique_text_list(existing_text_list: list[str], new_text_list: list[str]) -> list[str]:
    """Merge text lists while preserving first-seen order."""

    merged_text_list: list[str] = []
    seen_text_set: set[str] = set()

    for text in existing_text_list + new_text_list:
        if not text or text in seen_text_set:
            continue
        merged_text_list.append(text)
        seen_text_set.add(text)

    return merged_text_list


def build_multi_select_property(name_list: list[str]) -> dict[str, Any]:
    """Build a Notion multi-select property payload."""

    return {
        "multi_select": [
            {"name": name}
            for name in name_list
        ]
    }


def build_title_property(text: str) -> dict[str, Any]:
    """Build a Notion title property payload."""

    return {
        "title": [
            {
                "type": "text",
                "text": {"content": text[:2000]},
            }
        ]
    }


def build_rich_text_property(text: str) -> dict[str, Any]:
    """Build a Notion rich text property payload."""

    return {
        "rich_text": [
            {
                "type": "text",
                "text": {"content": text[:2000]},
            }
        ]
    }


def build_select_property(name: str) -> dict[str, Any]:
    """Build a Notion select property payload."""

    return {
        "select": {
            "name": name,
        }
    }


def build_optional_select_property(name: Optional[str]) -> dict[str, Any]:
    """Build a Notion select property payload that can be empty."""

    if not name:
        return {"select": None}

    return build_select_property(name)


def build_date_property(date_text: Optional[str]) -> dict[str, Any]:
    """Build a Notion date property payload."""

    if not date_text:
        return {"date": None}
    return {"date": {"start": date_text}}


def create_notion_page(
    config: Config,
    data_source_id: str,
    property_map: dict[str, Any],
    context: str,
    cover_url: Optional[str] = None,
    icon_url: Optional[str] = None,
) -> Optional[str]:
    """Create a Notion page, trying data source parent first and legacy database parent second."""

    parent_candidate_list = [
        {"type": "data_source_id", "data_source_id": data_source_id},
        {"data_source_id": data_source_id},
        {"database_id": data_source_id},
    ]
    last_result = RequestResult(False, {}, None, "No create attempt was made.")

    for parent in parent_candidate_list:
        body = {
            "parent": parent,
            "properties": property_map,
        }
        if cover_url:
            body["cover"] = build_external_cover(cover_url)
        if icon_url:
            body["icon"] = build_external_file_payload(icon_url)

        last_result = notion_request(config, "POST", "https://api.notion.com/v1/pages", json_body=body)

        if last_result.ok:
            page_id = last_result.data.get("id")
            if isinstance(page_id, str) and page_id:
                return page_id

            log_error(f"Created Notion page response did not include an id. context={context}")
            return None

        if last_result.status_code != 400 or "parent" not in last_result.error_message.lower():
            break

    log_error(
        f"Failed to create Notion page. context={context}, "
        f"status={last_result.status_code}, reason={last_result.error_message}"
    )
    return None


def update_notion_page(
    config: Config,
    page_id: str,
    property_map: dict[str, Any],
    context: str,
    cover_url: Optional[str] = None,
    icon_url: Optional[str] = None,
) -> bool:
    """Update an existing Notion page."""

    url = f"https://api.notion.com/v1/pages/{page_id}"
    body = {"properties": property_map}
    if cover_url:
        body["cover"] = build_external_cover(cover_url)
    if icon_url:
        body["icon"] = build_external_file_payload(icon_url)

    result = notion_request(config, "PATCH", url, json_body=body)

    if result.ok:
        return True

    log_error(
        f"Failed to update Notion page. context={context}, page_id={page_id}, "
        f"status={result.status_code}, reason={result.error_message}"
    )
    return False


def build_external_cover(cover_url: str) -> dict[str, Any]:
    """Build a Notion external cover payload."""

    return build_external_file_payload(cover_url)


def build_external_file_payload(file_url: str) -> dict[str, Any]:
    """Build a Notion external file payload."""

    return {
        "type": "external",
        "external": {
            "url": file_url,
        },
    }


def game_page_needs_update(
    steam_game: SteamGame,
    notion_game_page: NotionGamePage,
    include_playtime: bool,
    unrecord_playtime_minutes: Optional[int] = None,
) -> bool:
    """Return true when the Notion game row differs from the Steam snapshot."""

    metadata_changed = (
        steam_game.name != notion_game_page.name
        or steam_game.recent_played != notion_game_page.recent_played
        or steam_game.store_url != notion_game_page.store_url
        or (
            steam_game.header_image_url is not None
            and steam_game.header_image_url != notion_game_page.header_image_url
        )
        or (
            steam_game.icon_image_url is not None
            and steam_game.icon_image_url != notion_game_page.icon_image_url
        )
    )
    if metadata_changed:
        return True

    if (
        unrecord_playtime_minutes is not None
        and unrecord_playtime_minutes != notion_game_page.unrecord_playtime_minutes
    ):
        return True

    if not include_playtime:
        return False

    achievement_changed = False
    if steam_game.achievement_info is not None:
        achievement_changed = (
            steam_game.achievement_info.total_achievement != notion_game_page.total_achievement
            or steam_game.achievement_info.achieved_achievement != notion_game_page.achieved_achievement
        )

    return (
        steam_game.total_playtime_minutes != notion_game_page.total_playtime_minutes
        or achievement_changed
    )


def sync_game_list(
    config: Config,
    steam_game_list: list[SteamGame],
    notion_game_page_index: dict[int, NotionGamePage],
    include_playtime: bool,
    create_playtime_records: bool,
    is_initial_sync: bool,
) -> SyncStats:
    """Synchronize Steam game snapshots into Notion."""

    stats = SyncStats()
    today_text = date.today().isoformat()
    steam_game_index = {steam_game.app_id: steam_game for steam_game in steam_game_list}

    for steam_game in sorted(steam_game_list, key=lambda item: item.name.lower()):
        notion_game_page = notion_game_page_index.get(steam_game.app_id)

        if notion_game_page is None:
            sync_new_game(
                config,
                steam_game,
                today_text,
                include_playtime,
                create_playtime_records,
                is_initial_sync,
                stats,
            )
            continue

        sync_existing_game(
            config,
            steam_game,
            notion_game_page,
            today_text,
            include_playtime,
            create_playtime_records,
            is_initial_sync,
            stats,
        )

    extra_app_id_list = sorted(set(notion_game_page_index) - set(steam_game_index))
    for app_id in extra_app_id_list:
        notion_game_page = notion_game_page_index[app_id]
        stats.skipped_extra_notion_game_count += 1
        log_warning(
            "Notion has a game that is not in the Steam owned game list. "
            f"app_id={app_id}, name={notion_game_page.name}, page_id={notion_game_page.page_id}. "
            "No delete or archive action was performed."
        )

    return stats


def sync_new_game(
    config: Config,
    steam_game: SteamGame,
    today_text: str,
    include_playtime: bool,
    create_playtime_records: bool,
    is_initial_sync: bool,
    stats: SyncStats,
) -> None:
    """Create a new game row and its first playtime record."""

    context = build_game_context(steam_game)
    unrecord_playtime_minutes = get_new_game_unrecord_playtime_minutes(
        steam_game,
        include_playtime,
        create_playtime_records,
        is_initial_sync,
    )
    ensure_header_image_url(steam_game, None)
    created_game_page_id = create_notion_page(
        config,
        config.notion_game_data_source_id,
        build_game_properties(steam_game, include_playtime, unrecord_playtime_minutes),
        context,
        steam_game.header_image_url,
        steam_game.icon_image_url,
    )

    if created_game_page_id:
        stats.created_game_count += 1
        log_info(f"Created game row. {context}")
    else:
        stats.error_count += 1
        return

    if not create_playtime_records:
        log_info(f"Skipped initial playtime record because this run does not create playtime records. {context}")
        return

    if steam_game.total_playtime_minutes > NEW_GAME_UNRECORDED_PLAYTIME_THRESHOLD_MINUTES:
        log_info(
            "Skipped initial playtime record because new game playtime is treated as unrecorded history. "
            f"{context}, total_playtime_minutes={steam_game.total_playtime_minutes}"
        )
        return

    created_record = create_playtime_record(
        config,
        steam_game,
        steam_game.total_playtime_minutes,
        today_text,
        created_game_page_id,
    )
    if created_record:
        stats.created_playtime_record_count += 1
    else:
        stats.error_count += 1


def get_new_game_unrecord_playtime_minutes(
    steam_game: SteamGame,
    include_playtime: bool,
    create_playtime_records: bool,
    is_initial_sync: bool,
) -> Optional[int]:
    """Return UnrecordPlaytimeMinutes for a newly created game row."""

    if is_initial_sync:
        return steam_game.total_playtime_minutes

    if not include_playtime:
        return None

    if not create_playtime_records:
        return steam_game.total_playtime_minutes

    if steam_game.total_playtime_minutes > NEW_GAME_UNRECORDED_PLAYTIME_THRESHOLD_MINUTES:
        return steam_game.total_playtime_minutes

    return 0


def sync_existing_game(
    config: Config,
    steam_game: SteamGame,
    notion_game_page: NotionGamePage,
    today_text: str,
    include_playtime: bool,
    create_playtime_records: bool,
    is_initial_sync: bool,
    stats: SyncStats,
) -> None:
    """Update an existing game row and write a playtime delta when needed."""

    context = build_game_context(steam_game)
    delta_minutes = steam_game.total_playtime_minutes - notion_game_page.total_playtime_minutes
    unrecord_playtime_minutes = steam_game.total_playtime_minutes if is_initial_sync else None
    ensure_header_image_url(steam_game, notion_game_page.header_image_url)

    if game_page_needs_update(steam_game, notion_game_page, include_playtime, unrecord_playtime_minutes):
        updated = update_notion_page(
            config,
            notion_game_page.page_id,
            build_game_properties(steam_game, include_playtime, unrecord_playtime_minutes),
            context,
            steam_game.header_image_url,
            steam_game.icon_image_url,
        )
        if updated:
            stats.updated_game_count += 1
            log_info(f"Updated game row. {context}, delta_minutes={delta_minutes}")
        else:
            stats.error_count += 1
            return
    else:
        stats.unchanged_game_count += 1

    if not create_playtime_records:
        return

    if delta_minutes > 0:
        created_record = create_playtime_record(
            config,
            steam_game,
            delta_minutes,
            today_text,
            notion_game_page.page_id,
            notion_game_page,
        )
        if created_record:
            stats.created_playtime_record_count += 1
        else:
            stats.error_count += 1
    elif delta_minutes < 0:
        stats.error_count += 1
        log_error(
            "Steam total playtime is smaller than Notion history. "
            f"{context}, notion_minutes={notion_game_page.total_playtime_minutes}, "
            f"steam_minutes={steam_game.total_playtime_minutes}. "
            "Game row was updated if needed, but no playtime record was created."
        )


def sync_period_stat_page_list(
    config: Config,
    steam_game: SteamGame,
    delta_minutes: int,
    record_date: str,
    game_page_id: Optional[str],
) -> PeriodSyncResult:
    """Synchronize yearly and monthly period statistic pages for one playtime record."""

    if not config.notion_period_data_source_id:
        log_error(
            "Missing NOTION_PERIOD_DATA_SOURCE_ID. "
            f"Period statistic sync is skipped. {build_game_context(steam_game)}"
        )
        return build_empty_period_sync_result()

    if not game_page_id:
        log_error(
            "Missing game page id. Period statistic sync is skipped. "
            f"{build_game_context(steam_game)}"
        )
        return build_empty_period_sync_result()

    try:
        record_date_value = date.fromisoformat(record_date)
    except ValueError as error:
        log_error(
            "Invalid playtime record date. "
            f"{build_game_context(steam_game)}, record_date={record_date}, reason={error}"
        )
        return build_empty_period_sync_result()

    period_payload_list = build_period_payload_list(steam_game, record_date_value)
    period_page_id_list: list[str] = []
    year_summary_page_id: Optional[str] = None
    month_summary_page_id: Optional[str] = None

    for period_payload in period_payload_list:
        period_result = sync_period_stat_page(
            config,
            steam_game,
            delta_minutes,
            period_payload,
            game_page_id,
        )
        if period_result.period_page_id:
            period_page_id_list.append(period_result.period_page_id)
        if period_payload.period_type == PERIOD_TYPE_YEAR:
            year_summary_page_id = period_result.summary_page_id
        elif period_payload.period_type == PERIOD_TYPE_MONTH:
            month_summary_page_id = period_result.summary_page_id

    return PeriodSyncResult(
        period_page_id_list=period_page_id_list,
        year_summary_page_id=year_summary_page_id,
        month_summary_page_id=month_summary_page_id,
        year_text=period_payload_list[0].period_text,
        month_text=period_payload_list[1].period_text,
    )


def build_empty_period_sync_result() -> PeriodSyncResult:
    """Build an empty period sync result."""

    return PeriodSyncResult(
        period_page_id_list=[],
        year_summary_page_id=None,
        month_summary_page_id=None,
        year_text="",
        month_text="",
    )


def build_period_payload_list(steam_game: SteamGame, record_date_value: date) -> list[PeriodPayload]:
    """Build yearly and monthly period payloads for one game."""

    year = record_date_value.year
    month = record_date_value.month
    year_text = f"{year:04d}"
    month_text = f"{year:04d}-{month:02d}"

    return [
        PeriodPayload(
            period_id=f"{year_text}_{steam_game.app_id}",
            period_text=year_text,
            period_type=PERIOD_TYPE_YEAR,
            year=year,
            month=None,
            name=f"{year_text}_{steam_game.name}",
        ),
        PeriodPayload(
            period_id=f"{month_text}_{steam_game.app_id}",
            period_text=month_text,
            period_type=PERIOD_TYPE_MONTH,
            year=year,
            month=month,
            name=f"{month_text}_{steam_game.name}",
        ),
    ]


def sync_period_stat_page(
    config: Config,
    steam_game: SteamGame,
    delta_minutes: int,
    period_payload: PeriodPayload,
    game_page_id: str,
) -> PeriodStatSyncResult:
    """Create or update one period statistic page and return its page id."""

    summary_page_id = ensure_summary_page(config, period_payload)
    period_stat, can_create = find_period_stat_page(config, period_payload.period_id)
    if period_stat is None:
        if not can_create:
            return PeriodStatSyncResult(period_page_id=None, summary_page_id=summary_page_id)

        period_page_id = create_period_stat_page(config, steam_game, period_payload, game_page_id, summary_page_id)
        if period_page_id is None:
            return PeriodStatSyncResult(period_page_id=None, summary_page_id=summary_page_id)
        period_stat = PeriodStat(
            page_id=period_page_id,
            playtime_minutes=0,
            period_text=period_payload.period_text,
            period_type=period_payload.period_type,
        )

    new_playtime_minutes = period_stat.playtime_minutes + delta_minutes
    if update_period_stat_playtime(
        config,
        period_stat.page_id,
        new_playtime_minutes,
        period_payload.period_id,
        summary_page_id,
    ):
        return PeriodStatSyncResult(period_page_id=period_stat.page_id, summary_page_id=summary_page_id)

    return PeriodStatSyncResult(period_page_id=None, summary_page_id=summary_page_id)


def ensure_summary_page(config: Config, period_payload: PeriodPayload) -> Optional[str]:
    """Find or create a summary page for one period."""

    if not config.notion_summary_data_source_id:
        log_error(
            "Missing NOTION_SUMMARY_DATA_SOURCE_ID. "
            f"Summary sync is skipped for period={period_payload.period_text}, type={period_payload.period_type}."
        )
        return None

    page_list = query_summary_page_list(config, period_payload.period_text, period_payload.period_type)
    if page_list is None:
        return None

    if len(page_list) > 1:
        log_error(
            "Duplicate summary period found in summary data source. "
            f"period={period_payload.period_text}, type={period_payload.period_type}. "
            "Please merge duplicate rows manually."
        )
        return None

    if len(page_list) == 1:
        page_id = page_list[0].get("id")
        if isinstance(page_id, str) and page_id:
            return page_id
        log_warning(
            "Skipped summary page without page id. "
            f"period={period_payload.period_text}, type={period_payload.period_type}"
        )
        return None

    return create_summary_page(config, period_payload)


def create_summary_page(config: Config, period_payload: PeriodPayload) -> Optional[str]:
    """Create one monthly or yearly summary page."""

    context = f"summary_period={period_payload.period_text}, type={period_payload.period_type}"
    return create_notion_page(
        config,
        config.notion_summary_data_source_id,
        build_summary_properties(period_payload),
        context,
    )


def build_summary_properties(period_payload: PeriodPayload) -> dict[str, Any]:
    """Build Notion properties for one summary row."""

    return {
        "Period": build_title_property(period_payload.period_text),
        "Type": build_select_property(period_payload.period_type),
    }


def sync_summary_count_fields(config: Config) -> None:
    """Recompute and update numeric fields in the monthly and yearly summary table."""

    if not config.notion_summary_data_source_id:
        log_error(
            "Missing NOTION_SUMMARY_DATA_SOURCE_ID. "
            "Summary count field sync is skipped."
        )
        return

    game_page_list = query_all_notion_pages(config, config.notion_game_data_source_id)
    if game_page_list is None:
        log_error(
            "Failed to query Notion game data source for summary counts. "
            "Summary count field sync is skipped."
        )
        return

    period_page_list: list[dict[str, Any]] = []
    if config.notion_period_data_source_id:
        queried_period_page_list = query_all_notion_pages(config, config.notion_period_data_source_id)
        if queried_period_page_list is None:
            log_error(
                "Failed to query Notion period statistic data source for summary counts. "
                "PlayedGameNum will be counted as zero for this run."
            )
        else:
            period_page_list = queried_period_page_list
    else:
        log_warning(
            "Missing NOTION_PERIOD_DATA_SOURCE_ID. "
            "PlayedGameNum will be counted as zero for this run."
        )

    summary_page_id_index: dict[tuple[str, str], str] = {}
    duplicate_summary_key_set: set[tuple[str, str]] = set()
    summary_page_list = query_all_notion_pages(config, config.notion_summary_data_source_id)
    if summary_page_list is None:
        log_warning(
            "Failed to query summary data source before count update. "
            "Existing periods with zero current count cannot be reset in this run."
        )
    else:
        summary_page_id_index, duplicate_summary_key_set = build_summary_page_id_index(summary_page_list)

    notion_game_page_list = parse_notion_game_page_list(game_page_list)
    period_stat_list = parse_period_stat_page_list(period_page_list)
    summary_count_map = build_summary_count_map(notion_game_page_list, period_stat_list)
    target_key_set = set(summary_count_map) | set(summary_page_id_index)

    for summary_key in sorted(target_key_set, key=sort_summary_key):
        if summary_key in duplicate_summary_key_set:
            log_error(
                "Skipped summary count update because duplicate summary rows exist. "
                f"period={summary_key[1]}, type={summary_key[0]}."
            )
            continue

        summary_count = summary_count_map.get(summary_key, SummaryCount())
        summary_page_id = summary_page_id_index.get(summary_key)
        if summary_page_id is None:
            period_payload = build_summary_period_payload(summary_key[0], summary_key[1])
            if period_payload is None:
                continue
            summary_page_id = ensure_summary_page(config, period_payload)

        if summary_page_id is None:
            continue

        update_summary_count_page(config, summary_page_id, summary_key[1], summary_key[0], summary_count)


def parse_notion_game_page_list(page_list: list[dict[str, Any]]) -> list[NotionGamePage]:
    """Parse Notion game pages and skip pages without a valid AppID."""

    notion_game_page_list: list[NotionGamePage] = []
    for page in page_list:
        notion_game_page = parse_notion_game_page(page)
        if notion_game_page is None:
            page_id = str(page.get("id", "unknown"))
            log_warning(
                "Skipped Notion game page without valid AppID during summary count sync. "
                f"page_id={page_id}"
            )
            continue
        notion_game_page_list.append(notion_game_page)

    return notion_game_page_list


def parse_period_stat_page_list(page_list: list[dict[str, Any]]) -> list[PeriodStat]:
    """Parse Notion period statistic pages and skip invalid rows."""

    period_stat_list: list[PeriodStat] = []
    for page in page_list:
        period_stat = parse_period_stat_page(page)
        if period_stat is not None:
            period_stat_list.append(period_stat)

    return period_stat_list


def build_summary_page_id_index(
    page_list: list[dict[str, Any]],
) -> tuple[dict[tuple[str, str], str], set[tuple[str, str]]]:
    """Build a Period and Type keyed index from summary pages."""

    summary_page_id_index: dict[tuple[str, str], str] = {}
    duplicate_key_set: set[tuple[str, str]] = set()

    for page in page_list:
        summary_key = parse_summary_page_key(page)
        if summary_key is None:
            continue

        if summary_key in summary_page_id_index:
            duplicate_key_set.add(summary_key)
            log_error(
                "Duplicate summary row found while building summary count index. "
                f"period={summary_key[1]}, type={summary_key[0]}."
            )
            continue

        page_id = page.get("id")
        if isinstance(page_id, str) and page_id:
            summary_page_id_index[summary_key] = page_id

    for duplicate_key in duplicate_key_set:
        summary_page_id_index.pop(duplicate_key, None)

    return summary_page_id_index, duplicate_key_set


def parse_summary_page_key(page: dict[str, Any]) -> Optional[tuple[str, str]]:
    """Parse one summary page key as a Period and Type pair."""

    page_id = str(page.get("id", "unknown"))
    property_map = page.get("properties", {})
    if not isinstance(property_map, dict):
        log_warning(f"Skipped summary page without valid properties. page_id={page_id}")
        return None

    period_text = get_notion_title(property_map, "Period")
    period_type = get_notion_select_name(property_map, "Type")
    if not period_text or not period_type:
        log_warning(f"Skipped summary page without Period or Type. page_id={page_id}")
        return None

    return normalize_summary_key(period_type, period_text, f"summary_page_id={page_id}")


def build_summary_count_map(
    notion_game_page_list: list[NotionGamePage],
    period_stat_list: list[PeriodStat],
) -> dict[tuple[str, str], SummaryCount]:
    """Build full summary count values from game rows and period statistic rows."""

    summary_count_map: dict[tuple[str, str], SummaryCount] = {}

    for notion_game_page in notion_game_page_list:
        add_game_field_summary_count(
            summary_count_map,
            notion_game_page,
            notion_game_page.buy_year,
            notion_game_page.buy_month,
            NEW_GAME_NUM_PROPERTY_NAME,
            "buy",
        )
        add_game_field_summary_count(
            summary_count_map,
            notion_game_page,
            notion_game_page.complete_year,
            notion_game_page.complete_month,
            COMPLETE_GAME_NUM_PROPERTY_NAME,
            "complete",
        )
        add_game_field_summary_count(
            summary_count_map,
            notion_game_page,
            notion_game_page.full_achievement_year,
            notion_game_page.full_achievement_month,
            FULL_ACHIEVEMENT_GAME_NUM_PROPERTY_NAME,
            "full_achievement",
        )

    for period_stat in period_stat_list:
        if not period_stat.period_text or not period_stat.period_type:
            log_warning(
                "Skipped PlayedGameNum count for period statistic without Period or Type. "
                f"page_id={period_stat.page_id}"
            )
            continue

        summary_key = normalize_summary_key(
            period_stat.period_type,
            period_stat.period_text,
            f"period_stat_page_id={period_stat.page_id}",
        )
        if summary_key is None:
            continue

        summary_count = get_or_create_summary_count(summary_count_map, summary_key)
        summary_count.played_game_count += 1
        summary_count.total_playtime_minutes += period_stat.playtime_minutes

    return summary_count_map


def add_game_field_summary_count(
    summary_count_map: dict[tuple[str, str], SummaryCount],
    notion_game_page: NotionGamePage,
    year_text: Optional[str],
    month_text: Optional[str],
    count_property_name: str,
    field_group_name: str,
) -> None:
    """Add one game's year and month text fields to summary counts."""

    warn_if_year_month_mismatch(notion_game_page, year_text, month_text, field_group_name)

    if year_text:
        summary_key = normalize_summary_key(
            PERIOD_TYPE_YEAR,
            year_text,
            f"app_id={notion_game_page.app_id}, field={field_group_name}_year",
        )
        if summary_key is not None:
            increment_summary_count(summary_count_map, summary_key, count_property_name)

    if month_text:
        summary_key = normalize_summary_key(
            PERIOD_TYPE_MONTH,
            month_text,
            f"app_id={notion_game_page.app_id}, field={field_group_name}_month",
        )
        if summary_key is not None:
            increment_summary_count(summary_count_map, summary_key, count_property_name)


def warn_if_year_month_mismatch(
    notion_game_page: NotionGamePage,
    year_text: Optional[str],
    month_text: Optional[str],
    field_group_name: str,
) -> None:
    """Warn when a game's YYYY-MM text field points to a different year than its YYYY field."""

    if not year_text or not month_text:
        return

    year_text = year_text.strip()
    month_text = month_text.strip()
    if not is_valid_year_text(year_text) or not is_valid_month_text(month_text):
        return

    if month_text[:4] != year_text:
        log_warning(
            "Game year and month fields point to different years. "
            f"app_id={notion_game_page.app_id}, name={notion_game_page.name}, "
            f"field_group={field_group_name}, year={year_text}, month={month_text}. "
            "The values are counted independently and are not corrected automatically."
        )


def increment_summary_count(
    summary_count_map: dict[tuple[str, str], SummaryCount],
    summary_key: tuple[str, str],
    count_property_name: str,
) -> None:
    """Increment one numeric summary count by property name."""

    summary_count = get_or_create_summary_count(summary_count_map, summary_key)
    if count_property_name == NEW_GAME_NUM_PROPERTY_NAME:
        summary_count.new_game_count += 1
    elif count_property_name == COMPLETE_GAME_NUM_PROPERTY_NAME:
        summary_count.complete_game_count += 1
    elif count_property_name == FULL_ACHIEVEMENT_GAME_NUM_PROPERTY_NAME:
        summary_count.full_achievement_game_count += 1
    else:
        log_warning(f"Unknown summary count property name. property_name={count_property_name}")


def get_or_create_summary_count(
    summary_count_map: dict[tuple[str, str], SummaryCount],
    summary_key: tuple[str, str],
) -> SummaryCount:
    """Return an existing SummaryCount or create an empty one."""

    summary_count = summary_count_map.get(summary_key)
    if summary_count is None:
        summary_count = SummaryCount()
        summary_count_map[summary_key] = summary_count

    return summary_count


def normalize_summary_key(period_type: str, period_text: str, context: str) -> Optional[tuple[str, str]]:
    """Validate and normalize a Period and Type pair."""

    period_type = period_type.strip()
    period_text = period_text.strip()

    if period_type == PERIOD_TYPE_YEAR:
        if is_valid_year_text(period_text):
            return period_type, period_text
    elif period_type == PERIOD_TYPE_MONTH:
        if is_valid_month_text(period_text):
            return period_type, period_text
    else:
        log_warning(f"Skipped summary period with unsupported Type. context={context}, type={period_type}")
        return None

    log_warning(
        "Skipped summary period with invalid Period format. "
        f"context={context}, period={period_text}, type={period_type}."
    )
    return None


def is_valid_year_text(text: str) -> bool:
    """Return true when text is a YYYY year value."""

    return len(text) == 4 and text.isdigit()


def is_valid_month_text(text: str) -> bool:
    """Return true when text is a YYYY-MM month value."""

    if len(text) != 7 or text[4] != "-":
        return False

    year_text = text[:4]
    month_text = text[5:]
    if not year_text.isdigit() or not month_text.isdigit():
        return False

    month = int(month_text)
    return 1 <= month <= 12


def build_summary_period_payload(period_type: str, period_text: str) -> Optional[PeriodPayload]:
    """Build a PeriodPayload used to create a missing summary row."""

    summary_key = normalize_summary_key(period_type, period_text, "build_summary_period_payload")
    if summary_key is None:
        return None

    if period_type == PERIOD_TYPE_YEAR:
        year = int(period_text)
        month: Optional[int] = None
    else:
        year = int(period_text[:4])
        month = int(period_text[5:])

    return PeriodPayload(
        period_id=period_text,
        period_text=period_text,
        period_type=period_type,
        year=year,
        month=month,
        name=period_text,
    )


def update_summary_count_page(
    config: Config,
    page_id: str,
    period_text: str,
    period_type: str,
    summary_count: SummaryCount,
) -> bool:
    """Overwrite the four numeric count fields on one summary page."""

    return update_notion_page(
        config,
        page_id,
        build_summary_count_properties(summary_count),
        f"summary_count, period={period_text}, type={period_type}",
    )


def build_summary_count_properties(summary_count: SummaryCount) -> dict[str, Any]:
    """Build Notion properties for full recomputed summary counts."""

    return {
        NEW_GAME_NUM_PROPERTY_NAME: {"number": summary_count.new_game_count},
        COMPLETE_GAME_NUM_PROPERTY_NAME: {"number": summary_count.complete_game_count},
        FULL_ACHIEVEMENT_GAME_NUM_PROPERTY_NAME: {"number": summary_count.full_achievement_game_count},
        PLAYED_GAME_NUM_PROPERTY_NAME: {"number": summary_count.played_game_count},
        TOTAL_PLAYTIME_MINUTES_PROPERTY_NAME: {"number": summary_count.total_playtime_minutes},
    }


def sort_summary_key(summary_key: tuple[str, str]) -> tuple[str, int]:
    """Sort summary keys by Period text and then Year before Month."""

    period_type, period_text = summary_key
    type_order = 0 if period_type == PERIOD_TYPE_YEAR else 1
    return period_text, type_order


def find_period_stat_page(config: Config, period_id: str) -> tuple[Optional[PeriodStat], bool]:
    """Find one period statistic page by PeriodID and report whether creation is allowed."""

    page_list = query_period_stat_page_list(config, period_id)
    if page_list is None:
        return None, False

    if len(page_list) == 0:
        return None, True

    if len(page_list) > 1:
        log_error(
            "Duplicate PeriodID found in period statistic data source. "
            f"period_id={period_id}. Please merge duplicate rows manually."
        )
        return None, False

    period_stat = parse_period_stat_page(page_list[0])
    if period_stat is None:
        return None, False

    return period_stat, False


def create_period_stat_page(
    config: Config,
    steam_game: SteamGame,
    period_payload: PeriodPayload,
    game_page_id: str,
    summary_page_id: Optional[str],
) -> Optional[str]:
    """Create one period statistic page with zero initial playtime."""

    context = (
        f"period_id={period_payload.period_id}, "
        f"type={period_payload.period_type}, "
        f"{build_game_context(steam_game)}"
    )
    return create_notion_page(
        config,
        config.notion_period_data_source_id,
        build_period_stat_properties(steam_game, period_payload, game_page_id, summary_page_id, 0),
        context,
    )


def build_period_stat_properties(
    steam_game: SteamGame,
    period_payload: PeriodPayload,
    game_page_id: str,
    summary_page_id: Optional[str],
    playtime_minutes: int,
) -> dict[str, Any]:
    """Build Notion properties for one period statistic row."""

    property_map = {
        "Name": build_title_property(period_payload.name),
        "PeriodID": build_rich_text_property(period_payload.period_id),
        "Period": build_select_property(period_payload.period_text),
        "Type": build_select_property(period_payload.period_type),
        "Year": build_select_property(f"{period_payload.year:04d}"),
        "Month": build_optional_select_property(
            str(period_payload.month) if period_payload.month is not None else None
        ),
        "AppID": {"number": steam_game.app_id},
        "PlayTimeMinutes": {"number": playtime_minutes},
        GAME_LOG_RELATION_PROPERTY_NAME: build_relation_property(game_page_id),
    }

    if summary_page_id:
        property_map[SUMMARY_RELATION_PROPERTY_NAME] = build_relation_property(summary_page_id)

    return property_map


def update_period_stat_playtime(
    config: Config,
    page_id: str,
    playtime_minutes: int,
    period_id: str,
    summary_page_id: Optional[str] = None,
) -> bool:
    """Update PlayTimeMinutes for one period statistic page."""

    property_map = {"PlayTimeMinutes": {"number": playtime_minutes}}
    if summary_page_id:
        property_map[SUMMARY_RELATION_PROPERTY_NAME] = build_relation_property(summary_page_id)

    return update_notion_page(
        config,
        page_id,
        property_map,
        f"period_id={period_id}, playtime_minutes={playtime_minutes}",
    )


def update_game_summary_fields(
    config: Config,
    steam_game: SteamGame,
    game_page_id: Optional[str],
    notion_game_page: Optional[NotionGamePage],
    period_sync_result: PeriodSyncResult,
) -> bool:
    """Update game total table summary relation and grouping fields."""

    if not game_page_id:
        log_error(f"Missing game page id. Game summary fields are skipped. {build_game_context(steam_game)}")
        return False

    property_map = build_game_summary_properties(notion_game_page, period_sync_result)
    if not property_map:
        return True

    return update_notion_page(
        config,
        game_page_id,
        property_map,
        f"game_summary_fields, {build_game_context(steam_game)}",
    )


def build_game_summary_properties(
    notion_game_page: Optional[NotionGamePage],
    period_sync_result: PeriodSyncResult,
) -> dict[str, Any]:
    """Build game table properties that append summary relations and grouping tags."""

    property_map: dict[str, Any] = {}

    if period_sync_result.year_summary_page_id and period_sync_result.year_text:
        existing_year_summary_page_id_list = (
            notion_game_page.year_summary_page_id_list if notion_game_page is not None else []
        )
        existing_played_year_name_list = (
            notion_game_page.played_year_name_list if notion_game_page is not None else []
        )
        property_map[YEAR_SUMMARY_RELATION_PROPERTY_NAME] = build_relation_list_property(
            merge_unique_text_list(existing_year_summary_page_id_list, [period_sync_result.year_summary_page_id])
        )
        property_map[PLAYED_YEAR_PROPERTY_NAME] = build_multi_select_property(
            merge_unique_text_list(existing_played_year_name_list, [period_sync_result.year_text])
        )

    if period_sync_result.month_summary_page_id and period_sync_result.month_text:
        existing_month_summary_page_id_list = (
            notion_game_page.month_summary_page_id_list if notion_game_page is not None else []
        )
        existing_played_month_name_list = (
            notion_game_page.played_month_name_list if notion_game_page is not None else []
        )
        property_map[MONTH_SUMMARY_RELATION_PROPERTY_NAME] = build_relation_list_property(
            merge_unique_text_list(existing_month_summary_page_id_list, [period_sync_result.month_summary_page_id])
        )
        property_map[PLAYED_MONTH_PROPERTY_NAME] = build_multi_select_property(
            merge_unique_text_list(existing_played_month_name_list, [period_sync_result.month_text])
        )

    return property_map


def create_playtime_record(
    config: Config,
    steam_game: SteamGame,
    delta_minutes: int,
    today_text: str,
    game_page_id: Optional[str] = None,
    notion_game_page: Optional[NotionGamePage] = None,
) -> bool:
    """Create one playtime history record."""

    context = build_game_context(steam_game) + f", delta_minutes={delta_minutes}"
    period_sync_result = sync_period_stat_page_list(
        config,
        steam_game,
        delta_minutes,
        today_text,
        game_page_id,
    )
    created_page_id = create_notion_page(
        config,
        config.notion_playtime_data_source_id,
        build_playtime_properties(
            steam_game,
            delta_minutes,
            today_text,
            game_page_id,
            period_sync_result.period_page_id_list,
        ),
        context,
    )

    if created_page_id:
        log_info(f"Created playtime record. {context}")
        update_game_summary_fields(config, steam_game, game_page_id, notion_game_page, period_sync_result)

    return created_page_id is not None


def build_game_context(steam_game: SteamGame) -> str:
    """Build a stable short context string for logs."""

    return f"app_id={steam_game.app_id}, name={steam_game.name}"


def print_summary(stats: SyncStats) -> None:
    """Print sync summary counters."""

    log_info("Sync summary:")
    log_info(f"  created_game_count={stats.created_game_count}")
    log_info(f"  updated_game_count={stats.updated_game_count}")
    log_info(f"  unchanged_game_count={stats.unchanged_game_count}")
    log_info(f"  created_playtime_record_count={stats.created_playtime_record_count}")
    log_info(f"  skipped_extra_notion_game_count={stats.skipped_extra_notion_game_count}")
    log_info(f"  error_count={stats.error_count}")


def run_sync() -> None:
    """Run the full Steam to Notion sync once."""

    config = load_config()
    if config is None:
        return

    state_file_path = get_state_file_path()
    sync_state = load_sync_state(state_file_path)
    today_text = date.today().isoformat()
    is_initial_sync = not sync_state.has_known_last_update_date
    is_same_day_repeat = sync_state.has_known_last_update_date and sync_state.last_update_date == today_text
    include_playtime = not is_same_day_repeat
    create_playtime_records = include_playtime and not is_initial_sync

    if is_initial_sync:
        log_info(
            "First initialization is enabled for this run. "
            f"last_update_date={sync_state.last_update_date}, current_date={today_text}. "
            "Game totals and achievements will be synced, but no playtime records will be created."
        )
    elif include_playtime:
        log_info(
            "Playtime sync is enabled for this run. "
            f"last_update_date={sync_state.last_update_date}, current_date={today_text}"
        )
    else:
        log_info(
            "Playtime sync is skipped because current date was already processed. "
            f"last_update_date={sync_state.last_update_date}, current_date={today_text}"
        )

    steam_game_list = fetch_steam_game_list(config)
    if steam_game_list is None:
        return

    notion_page_list = query_all_notion_pages(config, config.notion_game_data_source_id)
    if notion_page_list is None:
        log_error(
            "Failed to read Notion game data source. "
            "Check NOTION_GAME_DATA_SOURCE_ID, integration permissions, and Notion field schema."
        )
        return

    stats = SyncStats()
    notion_game_page_index = build_notion_game_page_index(notion_page_list, stats)
    stats.error_count += sync_recent_played_for_all_notion_games(
        config,
        notion_game_page_index,
        steam_game_list,
    )

    if include_playtime:
        update_game_achievement_info_list(config, steam_game_list)
    else:
        log_info("Achievement sync is skipped because current date was already processed.")

    sync_stats = sync_game_list(
        config,
        steam_game_list,
        notion_game_page_index,
        include_playtime,
        create_playtime_records,
        is_initial_sync,
    )
    sync_stats.error_count += stats.error_count

    if include_playtime:
        save_sync_state(
            state_file_path,
            SyncState(last_update_date=today_text, has_known_last_update_date=True),
        )

    sync_summary_count_fields(config)
    print_summary(sync_stats)


def main() -> None:
    """Program entrypoint that keeps GitHub Actions exit status successful."""

    try:
        run_sync()
    except Exception as error:
        log_error(f"Unexpected top-level error was caught: {error}")


if __name__ == "__main__":
    main()
