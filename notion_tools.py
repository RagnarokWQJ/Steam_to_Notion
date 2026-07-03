"""Small Notion maintenance tools for the Steam sync data sources."""

from __future__ import annotations

import argparse
import csv
import re
import os
import time
from dataclasses import dataclass
from datetime import date
from datetime import datetime
from typing import Any
from typing import Optional
from typing import Sequence

import requests


NOTION_VERSION_DEFAULT = "2025-09-03"
REQUEST_TIMEOUT_SECONDS = 30
MAX_RETRY_COUNT = 3
RETRY_STATUS_CODE_SET = {429, 500, 502, 503, 504}
GAME_LOG_RELATION_PROPERTY_NAME = "GameLogRelation"
PERIOD_RELATION_PROPERTY_NAME = "PeriodRelation"
SUMMARY_RELATION_PROPERTY_NAME = "SummaryRelation"
MONTH_SUMMARY_RELATION_PROPERTY_NAME = "MonthSummaryRelation"
YEAR_SUMMARY_RELATION_PROPERTY_NAME = "YearSummaryRelation"
PLAYED_YEAR_PROPERTY_NAME = "PlayedYear"
PLAYED_MONTH_PROPERTY_NAME = "PlayedMonth"
NEW_GAME_NUM_PROPERTY_NAME = "NewGameNum"
COMPLETE_GAME_NUM_PROPERTY_NAME = "CompleteGameNum"
FULL_ACHIEVEMENT_GAME_NUM_PROPERTY_NAME = "FullAchievementGameNum"
PLAYED_GAME_NUM_PROPERTY_NAME = "PlayedGameNum"
TOTAL_PLAYTIME_MINUTES_PROPERTY_NAME = "TotalPlayTimeMinutes"
BUY_YEAR_PROPERTY_NAME = "BuyYear"
BUY_MONTH_PROPERTY_NAME = "BuyMonth"
COMPLETE_YEAR_PROPERTY_NAME = "CompleteYear"
COMPLETE_MONTH_PROPERTY_NAME = "CompleteMonth"
FULL_ACHIEVEMENT_YEAR_PROPERTY_NAME = "FullAchievementYear"
FULL_ACHIEVEMENT_MONTH_PROPERTY_NAME = "FullAchievementMonth"
PERIOD_TYPE_YEAR = "Year"
PERIOD_TYPE_MONTH = "Month"
ENTRY_DATE_PROPERTY_NAME = "入库日期"
DEFAULT_GAME_NAME_COLUMN = "游戏名"
DEFAULT_ENTRY_DATE_COLUMN = "入库日期"
CHINESE_ENTRY_DATE_PATTERN = re.compile(r"^(\d{4}) 年 (\d{1,2}) 月 (\d{1,2}) 日$")
ENGLISH_ENTRY_DATE_PATTERN = re.compile(r"^(\d{1,2}) ([A-Z][a-z]{2}) (\d{4})$")
GAME_NAME_PARENTHESES_PATTERN = re.compile(r"\s*[\(（][^()（）]*[\)）]\s*")
ENGLISH_MONTH_NAME_TO_NUMBER = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}


@dataclass
class NotionConfig:
    """Runtime configuration for Notion maintenance tools."""

    notion_api_key: str
    notion_version: str
    command: Optional[str] = None
    game_data_source_id: Optional[str] = None
    playtime_data_source_id: Optional[str] = None
    period_data_source_id: Optional[str] = None
    summary_data_source_id: Optional[str] = None
    data_source_id: Optional[str] = None
    template_id: Optional[str] = None
    erase_content: bool = False
    dry_run: bool = False
    keep_extra_period_stats: bool = False
    csv_file_path: Optional[str] = None
    game_name_column: str = DEFAULT_GAME_NAME_COLUMN
    entry_date_column: str = DEFAULT_ENTRY_DATE_COLUMN


@dataclass
class RequestResult:
    """HTTP request result used to keep tool steps from raising directly."""

    ok: bool
    data: dict[str, Any]
    status_code: Optional[int]
    error_message: str


@dataclass
class GamePageIndex:
    """AppID keyed game page index and duplicate AppID set."""

    app_id_to_page_id: dict[int, str]
    duplicate_app_id_set: set[int]


@dataclass
class EntryDateRow:
    """CSV row prepared for entry date synchronization."""

    row_number: int
    game_name: str
    entry_date: str
    row_data: dict[str, Any]


@dataclass
class RebuildGamePage:
    """Game table page data used by derived statistic rebuild."""

    page_id: str
    app_id: int
    name: str
    buy_year: Optional[str]
    buy_month: Optional[str]
    complete_year: Optional[str]
    complete_month: Optional[str]
    full_achievement_year: Optional[str]
    full_achievement_month: Optional[str]


@dataclass
class RebuildGameIndex:
    """AppID keyed game page data and duplicate AppID set."""

    app_id_to_game_page: dict[int, RebuildGamePage]
    duplicate_app_id_set: set[int]


@dataclass
class PlaytimeRecord:
    """Playtime history page parsed for derived statistic rebuild."""

    page_id: str
    app_id: int
    record_date: Optional[date]
    delta_minutes: Optional[int]
    game_page: RebuildGamePage


@dataclass
class PeriodAggregate:
    """Aggregated playtime for one game in one year or month period."""

    period_id: str
    period_text: str
    period_type: str
    year: int
    month: Optional[int]
    app_id: int
    game_name: str
    game_page_id: str
    playtime_minutes: int = 0
    playtime_page_id_list: Optional[list[str]] = None
    period_page_id: Optional[str] = None
    summary_page_id: Optional[str] = None


@dataclass
class PeriodStatPage:
    """Existing monthly or yearly period statistic page."""

    page_id: str
    period_id: str
    period_text: Optional[str]
    period_type: Optional[str]
    playtime_minutes: int


@dataclass
class SummaryCount:
    """Full recomputed summary count values for one period."""

    new_game_count: int = 0
    complete_game_count: int = 0
    full_achievement_game_count: int = 0
    played_game_count: int = 0
    total_playtime_minutes: int = 0


@dataclass
class SummaryPageInfo:
    """Summary page id and current numeric count values."""

    page_id: str
    summary_count: SummaryCount


@dataclass
class RebuildStats:
    """Counters for the derived statistic rebuild command."""

    created_period_count: int = 0
    updated_period_count: int = 0
    archived_period_count: int = 0
    updated_playtime_count: int = 0
    updated_game_count: int = 0
    created_summary_count: int = 0
    updated_summary_count: int = 0
    unchanged_summary_count: int = 0
    skipped_count: int = 0
    error_count: int = 0


def log_info(message: str) -> None:
    """Print an informational log line."""

    print(f"[INFO] {message}")


def log_warning(message: str) -> None:
    """Print a warning log line."""

    print(f"[WARN] {message}")


def log_error(message: str) -> None:
    """Print an error log line."""

    print(f"[ERROR] {message}")


def parse_retry_after(retry_after_text: Optional[str], fallback_seconds: int) -> int:
    """Parse a Retry-After header value."""

    if retry_after_text is None:
        return fallback_seconds

    try:
        retry_after_seconds = int(retry_after_text)
    except ValueError:
        return fallback_seconds

    return max(1, retry_after_seconds)


def send_http_request(
    method: str,
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    json_body: Optional[dict[str, Any]] = None,
) -> RequestResult:
    """Send an HTTP request with small retry handling and no uncaught exception."""

    for attempt_index in range(1, MAX_RETRY_COUNT + 1):
        try:
            response = requests.request(
                method=method,
                url=url,
                headers=headers,
                json=json_body,
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


def notion_headers(config: NotionConfig) -> dict[str, str]:
    """Build Notion request headers."""

    return {
        "Authorization": f"Bearer {config.notion_api_key}",
        "Content-Type": "application/json",
        "Notion-Version": config.notion_version,
    }


def notion_request(
    config: NotionConfig,
    method: str,
    url: str,
    *,
    json_body: Optional[dict[str, Any]] = None,
) -> RequestResult:
    """Send a Notion API request."""

    return send_http_request(method, url, headers=notion_headers(config), json_body=json_body)


def query_all_notion_pages(config: NotionConfig, data_source_id: str) -> Optional[list[dict[str, Any]]]:
    """Query every page in one Notion data source or legacy database."""

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


def query_all_notion_pages_from_url(config: NotionConfig, url: str) -> Optional[list[dict[str, Any]]]:
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


def get_notion_number(property_map: dict[str, Any], property_name: str) -> Optional[float]:
    """Read a Notion number property."""

    number_value = property_map.get(property_name, {}).get("number")
    if isinstance(number_value, (int, float)):
        return float(number_value)
    return None


def get_notion_title(property_map: dict[str, Any], property_name: str) -> Optional[str]:
    """Read a Notion title property as plain text."""

    title_item_list = property_map.get(property_name, {}).get("title", [])
    if not isinstance(title_item_list, list):
        return None

    text_part_list: list[str] = []
    for title_item in title_item_list:
        if not isinstance(title_item, dict):
            continue
        plain_text = title_item.get("plain_text")
        if plain_text:
            text_part_list.append(str(plain_text))

    title_text = "".join(text_part_list)
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


def get_notion_select_name(property_map: dict[str, Any], property_name: str) -> Optional[str]:
    """Read a Notion select property option name."""

    select_value = property_map.get(property_name, {}).get("select")
    if not isinstance(select_value, dict):
        return None

    name = select_value.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def get_notion_relation_id_list(property_map: dict[str, Any], property_name: str) -> list[str]:
    """Read a Notion relation property as page ids."""

    relation_item_list = property_map.get(property_name, {}).get("relation", [])
    if not isinstance(relation_item_list, list):
        return []

    page_id_list: list[str] = []
    for relation_item in relation_item_list:
        if not isinstance(relation_item, dict):
            continue
        page_id = relation_item.get("id")
        if isinstance(page_id, str) and page_id:
            page_id_list.append(page_id)

    return page_id_list


def get_notion_date(property_map: dict[str, Any], property_name: str) -> Optional[date]:
    """Read a Notion date property start value as a date."""

    date_value = property_map.get(property_name, {}).get("date")
    if not isinstance(date_value, dict):
        return None

    start_text = date_value.get("start")
    if not isinstance(start_text, str) or not start_text.strip():
        return None

    return parse_notion_date_text(start_text.strip())


def parse_notion_date_text(date_text: str) -> Optional[date]:
    """Parse a Notion date or datetime text into a date."""

    if "T" not in date_text:
        try:
            return date.fromisoformat(date_text)
        except ValueError:
            return None

    normalized_text = date_text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized_text).date()
    except ValueError:
        return None


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
            return str(number_value)
        return None

    if formula_type == "string":
        string_value = formula_value.get("string")
        if isinstance(string_value, str) and string_value.strip():
            return string_value.strip()
        return None

    return None


def get_notion_formula_year_text(property_map: dict[str, Any], property_name: str) -> Optional[str]:
    """Read a Notion formula property and normalize it as YYYY."""

    formula_text = get_notion_formula_value_text(property_map, property_name)
    if formula_text is None:
        return None

    if len(formula_text) == 4 and formula_text.isdigit():
        return formula_text

    log_warning(f"Skipped invalid formula year. property={property_name}, value={formula_text}")
    return None


def get_notion_formula_month_text(property_map: dict[str, Any], property_name: str) -> Optional[str]:
    """Read a Notion formula property and normalize it as YYYY-MM."""

    formula_text = get_notion_formula_value_text(property_map, property_name)
    if formula_text is None:
        return None

    if len(formula_text) == 7 and formula_text[4] == "-" and formula_text[:4].isdigit():
        month_text = formula_text[5:]
        if month_text.isdigit() and 1 <= int(month_text) <= 12:
            return formula_text

    if len(formula_text) == 6 and formula_text.isdigit():
        normalized_text = f"{formula_text[:4]}-{formula_text[4:]}"
        month_text = normalized_text[5:]
        if 1 <= int(month_text) <= 12:
            return normalized_text

    log_warning(f"Skipped invalid formula month. property={property_name}, value={formula_text}")
    return None


def get_page_app_id(page: dict[str, Any]) -> Optional[int]:
    """Read the AppID number from one Notion page."""

    property_map = page.get("properties", {})
    if not isinstance(property_map, dict):
        return None

    app_id_number = get_notion_number(property_map, "AppID")
    if app_id_number is None:
        return None

    return int(app_id_number)


def get_page_id(page: dict[str, Any]) -> Optional[str]:
    """Read the Notion page id from one page object."""

    page_id = page.get("id")
    if isinstance(page_id, str) and page_id:
        return page_id
    return None


def get_page_name(page: dict[str, Any]) -> Optional[str]:
    """Read the Name title from one Notion page."""

    property_map = page.get("properties", {})
    if not isinstance(property_map, dict):
        return None

    return get_notion_title(property_map, "Name")


def build_game_page_index(game_page_list: list[dict[str, Any]]) -> GamePageIndex:
    """Build an AppID to game page id index from game table pages."""

    app_id_to_page_id: dict[int, str] = {}
    duplicate_app_id_set: set[int] = set()

    for page in game_page_list:
        page_id = get_page_id(page)
        if page_id is None:
            log_warning("Skipped game page without page id.")
            continue

        app_id = get_page_app_id(page)
        if app_id is None:
            log_warning(f"Skipped game page without valid AppID. page_id={page_id}")
            continue

        if app_id in app_id_to_page_id:
            duplicate_app_id_set.add(app_id)
            log_error(
                "Duplicate AppID found in game table. "
                f"app_id={app_id}, first_page_id={app_id_to_page_id[app_id]}, "
                f"duplicate_page_id={page_id}. Related playtime rows will be skipped."
            )
            continue

        app_id_to_page_id[app_id] = page_id

    for app_id in duplicate_app_id_set:
        app_id_to_page_id.pop(app_id, None)

    return GamePageIndex(
        app_id_to_page_id=app_id_to_page_id,
        duplicate_app_id_set=duplicate_app_id_set,
    )


def build_relation_property(game_page_id: str) -> dict[str, Any]:
    """Build a Notion GameLogRelation property payload."""

    return build_relation_list_property([game_page_id])


def build_relation_list_property(page_id_list: list[str]) -> dict[str, Any]:
    """Build a Notion relation property payload."""

    return {
        "relation": [
            {"id": page_id}
            for page_id in page_id_list
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


def build_multi_select_property(name_list: list[str]) -> dict[str, Any]:
    """Build a Notion multi-select property payload."""

    return {
        "multi_select": [
            {"name": name}
            for name in name_list
        ]
    }


def update_notion_page(
    config: NotionConfig,
    page_id: str,
    property_map: dict[str, Any],
    context: str,
) -> bool:
    """Update one Notion page property map."""

    url = f"https://api.notion.com/v1/pages/{page_id}"
    result = notion_request(config, "PATCH", url, json_body={"properties": property_map})
    if result.ok:
        return True

    log_error(
        "Failed to update Notion page. "
        f"context={context}, page_id={page_id}, status={result.status_code}, reason={result.error_message}"
    )
    return False


def archive_notion_page(config: NotionConfig, page_id: str, context: str) -> bool:
    """Archive one Notion page."""

    url = f"https://api.notion.com/v1/pages/{page_id}"
    result = notion_request(config, "PATCH", url, json_body={"archived": True})
    if result.ok:
        return True

    log_error(
        "Failed to archive Notion page. "
        f"context={context}, page_id={page_id}, status={result.status_code}, reason={result.error_message}"
    )
    return False


def create_notion_page(
    config: NotionConfig,
    data_source_id: str,
    property_map: dict[str, Any],
    context: str,
) -> Optional[str]:
    """Create a Notion page in one data source or legacy database."""

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
            "template": {"type": "default"},
        }
        last_result = notion_request(config, "POST", "https://api.notion.com/v1/pages", json_body=body)

        if not last_result.ok and should_retry_create_without_default_template(last_result):
            log_warning(
                "Failed to create Notion page with default template. "
                f"Retrying without template. context={context}, "
                f"status={last_result.status_code}, reason={last_result.error_message}"
            )
            fallback_body = {
                "parent": parent,
                "properties": property_map,
            }
            last_result = notion_request(
                config,
                "POST",
                "https://api.notion.com/v1/pages",
                json_body=fallback_body,
            )

        if last_result.ok:
            page_id = last_result.data.get("id")
            if isinstance(page_id, str) and page_id:
                return page_id

            log_error(f"Created Notion page response did not include an id. context={context}")
            return None

        if last_result.status_code != 400 or "parent" not in last_result.error_message.lower():
            break

    log_error(
        "Failed to create Notion page. "
        f"context={context}, status={last_result.status_code}, reason={last_result.error_message}"
    )
    return None


def should_retry_create_without_default_template(result: RequestResult) -> bool:
    """Return true when a create-page failure is likely caused by default template handling."""

    return result.status_code == 400 and "template" in result.error_message.lower()


def update_page_relation(config: NotionConfig, playtime_page_id: str, game_page_id: str) -> bool:
    """Update one playtime page GameLogRelation property to the matching game page."""

    url = f"https://api.notion.com/v1/pages/{playtime_page_id}"
    body = {
        "properties": {
            GAME_LOG_RELATION_PROPERTY_NAME: build_relation_property(game_page_id),
        }
    }
    result = notion_request(config, "PATCH", url, json_body=body)

    if result.ok:
        return True

    log_error(
        "Failed to update playtime GameLogRelation. "
        f"playtime_page_id={playtime_page_id}, game_page_id={game_page_id}, "
        f"status={result.status_code}, reason={result.error_message}"
    )
    return False


def run_repair_relations(config: NotionConfig) -> None:
    """Repair GameLogRelation values for every matching playtime history page."""

    if not config.game_data_source_id or not config.playtime_data_source_id:
        log_error("Missing --game-data-source-id or --playtime-data-source-id.")
        return

    log_info(
        "Starting repair-relations. "
        f"game_data_source_id={config.game_data_source_id}, "
        f"playtime_data_source_id={config.playtime_data_source_id}"
    )
    game_page_list = query_all_notion_pages(config, config.game_data_source_id)
    if game_page_list is None:
        log_error(
            "Failed to read game data source. "
            "Check --game-data-source-id, integration permissions, and the Notion API key."
        )
        return

    playtime_page_list = query_all_notion_pages(config, config.playtime_data_source_id)
    if playtime_page_list is None:
        log_error(
            "Failed to read playtime data source. "
            "Check --playtime-data-source-id, integration permissions, and the Notion API key."
        )
        return

    log_info(
        "Loaded Notion pages for relation repair. "
        f"game_page_count={len(game_page_list)}, "
        f"playtime_page_count={len(playtime_page_list)}"
    )
    game_page_index = build_game_page_index(game_page_list)
    updated_count = 0
    skipped_count = 0
    error_count = 0

    for playtime_page in playtime_page_list:
        playtime_page_id = get_page_id(playtime_page)
        if playtime_page_id is None:
            skipped_count += 1
            log_warning("Skipped playtime page without page id.")
            continue

        app_id = get_page_app_id(playtime_page)
        if app_id is None:
            skipped_count += 1
            log_warning(f"Skipped playtime page without valid AppID. page_id={playtime_page_id}")
            continue

        if app_id in game_page_index.duplicate_app_id_set:
            skipped_count += 1
            log_error(
                "Skipped playtime page because game AppID is duplicated. "
                f"app_id={app_id}, playtime_page_id={playtime_page_id}"
            )
            continue

        game_page_id = game_page_index.app_id_to_page_id.get(app_id)
        if game_page_id is None:
            skipped_count += 1
            log_warning(
                "Skipped playtime page because no matching game page was found. "
                f"app_id={app_id}, playtime_page_id={playtime_page_id}"
            )
            continue

        if update_page_relation(config, playtime_page_id, game_page_id):
            updated_count += 1
            log_info(
                "Updated playtime GameLogRelation. "
                f"app_id={app_id}, playtime_page_id={playtime_page_id}, game_page_id={game_page_id}"
            )
        else:
            error_count += 1

    log_info(
        "Repair GameLogRelation summary: "
        f"game_page_count={len(game_page_list)}, "
        f"playtime_page_count={len(playtime_page_list)}, "
        f"updated_count={updated_count}, "
        f"skipped_count={skipped_count}, "
        f"error_count={error_count}"
    )


def build_apply_template_body(template_id: str, erase_content: bool) -> dict[str, Any]:
    """Build the Notion page template apply payload."""

    return {
        "template": {
            "type": "template_id",
            "template_id": template_id,
        },
        "erase_content": erase_content,
    }


def apply_template_to_page(
    config: NotionConfig,
    page_id: str,
    template_id: str,
    erase_content: bool,
) -> bool:
    """Apply one Notion database page template to one page."""

    url = f"https://api.notion.com/v1/pages/{page_id}"
    body = build_apply_template_body(template_id, erase_content)
    result = notion_request(config, "PATCH", url, json_body=body)

    if result.ok:
        return True

    log_error(
        "Failed to apply template to page. "
        f"page_id={page_id}, template_id={template_id}, erase_content={erase_content}, "
        f"status={result.status_code}, reason={result.error_message}"
    )
    return False


def run_apply_template(config: NotionConfig) -> None:
    """Apply one Notion template to every page in one data source."""

    if not config.data_source_id or not config.template_id:
        log_error("Missing --data-source-id or --template-id.")
        return

    log_info(
        "Starting apply-template. "
        f"data_source_id={config.data_source_id}, template_id={config.template_id}, "
        f"erase_content={config.erase_content}"
    )
    page_list = query_all_notion_pages(config, config.data_source_id)
    if page_list is None:
        log_error(
            "Failed to read target data source. "
            "Check --data-source-id, integration permissions, and the Notion API key."
        )
        return

    log_info(f"Loaded target pages for template apply. page_count={len(page_list)}")
    updated_count = 0
    skipped_count = 0
    error_count = 0

    for page in page_list:
        page_id = get_page_id(page)
        if page_id is None:
            skipped_count += 1
            log_warning("Skipped target page without page id.")
            continue

        if apply_template_to_page(config, page_id, config.template_id, config.erase_content):
            updated_count += 1
            log_info(
                "Applied template to page. "
                f"page_id={page_id}, template_id={config.template_id}, erase_content={config.erase_content}"
            )
        else:
            error_count += 1

    log_info(
        "Apply template summary: "
        f"page_count={len(page_list)}, "
        f"updated_count={updated_count}, "
        f"skipped_count={skipped_count}, "
        f"error_count={error_count}, "
        f"erase_content={config.erase_content}"
    )


def parse_entry_date(date_text: str) -> Optional[str]:
    """Parse a supported entry date text into an ISO date string."""

    parsed_date = parse_chinese_entry_date(date_text)
    if parsed_date is not None:
        return parsed_date

    return parse_english_entry_date(date_text)


def parse_chinese_entry_date(date_text: str) -> Optional[str]:
    """Parse 'YYYY 年 M 月 D 日' text into an ISO date string."""

    match = CHINESE_ENTRY_DATE_PATTERN.fullmatch(date_text)
    if match is None:
        return None

    year = int(match.group(1))
    month = int(match.group(2))
    day = int(match.group(3))

    return build_iso_date(year, month, day)


def parse_english_entry_date(date_text: str) -> Optional[str]:
    """Parse 'D Mon YYYY' text into an ISO date string."""

    match = ENGLISH_ENTRY_DATE_PATTERN.fullmatch(date_text)
    if match is None:
        return None

    day = int(match.group(1))
    month_name = match.group(2)
    year = int(match.group(3))
    month = ENGLISH_MONTH_NAME_TO_NUMBER.get(month_name)
    if month is None:
        return None

    return build_iso_date(year, month, day)


def build_iso_date(year: int, month: int, day: int) -> Optional[str]:
    """Build an ISO date string if the year, month, and day are valid."""

    try:
        parsed_date = date(year, month, day)
    except ValueError:
        return None

    return parsed_date.isoformat()


def normalize_game_name_for_entry_date_match(game_name: str) -> str:
    """Remove parenthesized text from a game name before exact matching."""

    name_without_parentheses = GAME_NAME_PARENTHESES_PATTERN.sub(" ", game_name)
    return name_without_parentheses.strip()


def format_csv_row(row: EntryDateRow) -> str:
    """Format a CSV row for detailed logs."""

    return f"row_number={row.row_number}, row_data={row.row_data}"


def normalize_csv_cell(value: Any) -> str:
    """Normalize one CSV cell value to stripped text."""

    if value is None:
        return ""

    return str(value).strip()


def load_entry_date_csv(config: NotionConfig) -> dict[str, EntryDateRow]:
    """Load valid entry date rows from a CSV file keyed by game name."""

    if not config.csv_file_path:
        log_error("Missing --csv-file-path.")
        return {}

    log_info(
        "Loading entry date CSV. "
        f"path={config.csv_file_path}, game_name_column={config.game_name_column}, "
        f"entry_date_column={config.entry_date_column}"
    )
    row_by_game_name: dict[str, EntryDateRow] = {}
    conflict_game_name_set: set[str] = set()

    try:
        with open(config.csv_file_path, "r", encoding="utf-8-sig", newline="") as csv_file:
            reader = csv.DictReader(csv_file)
            if reader.fieldnames is None:
                log_error(f"CSV file has no header row. path={config.csv_file_path}")
                return {}

            if config.game_name_column not in reader.fieldnames:
                log_error(
                    "CSV file is missing game name column. "
                    f"path={config.csv_file_path}, column={config.game_name_column}"
                )
                return {}

            if config.entry_date_column not in reader.fieldnames:
                log_error(
                    "CSV file is missing entry date column. "
                    f"path={config.csv_file_path}, column={config.entry_date_column}"
                )
                return {}

            for row_number, row_data in enumerate(reader, start=2):
                row = parse_entry_date_csv_row(config, row_number, row_data)
                if row is None:
                    continue

                existing_row = row_by_game_name.get(row.game_name)
                if existing_row is None:
                    if row.game_name not in conflict_game_name_set:
                        row_by_game_name[row.game_name] = row
                    continue

                if existing_row.entry_date == row.entry_date:
                    continue

                conflict_game_name_set.add(row.game_name)
                row_by_game_name.pop(row.game_name, None)
                log_error(
                    "Skipped CSV game because duplicate rows contain different entry dates. "
                    f"game_name={row.game_name}, first_row={format_csv_row(existing_row)}, "
                    f"conflict_row={format_csv_row(row)}"
                )
    except OSError as error:
        log_error(f"Failed to read CSV file. path={config.csv_file_path}, reason={error}")
        return {}

    log_info(
        "Loaded valid CSV entry date rows. "
        f"valid_game_count={len(row_by_game_name)}, conflict_game_count={len(conflict_game_name_set)}"
    )
    return row_by_game_name


def parse_entry_date_csv_row(
    config: NotionConfig,
    row_number: int,
    row_data: dict[str, Any],
) -> Optional[EntryDateRow]:
    """Parse one CSV row into an EntryDateRow."""

    raw_game_name = normalize_csv_cell(row_data.get(config.game_name_column))
    game_name = normalize_game_name_for_entry_date_match(raw_game_name)
    raw_entry_date = normalize_csv_cell(row_data.get(config.entry_date_column))
    log_row = EntryDateRow(row_number=row_number, game_name=game_name, entry_date="", row_data=dict(row_data))

    if not game_name:
        log_warning(f"Skipped CSV row without game name. {format_csv_row(log_row)}")
        return None

    if not raw_entry_date:
        log_warning(f"Skipped CSV row without entry date. {format_csv_row(log_row)}")
        return None

    entry_date = parse_entry_date(raw_entry_date)
    if entry_date is None:
        log_error(
            "Skipped CSV row with invalid entry date. "
            "expected_format='YYYY 年 M 月 D 日' or 'D Mon YYYY', "
            f"value={raw_entry_date}, {format_csv_row(log_row)}"
        )
        return None

    return EntryDateRow(
        row_number=row_number,
        game_name=game_name,
        entry_date=entry_date,
        row_data=dict(row_data),
    )


def build_game_name_to_page_id_list(page_list: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Build an exact Name to page id list index from Notion pages."""

    game_name_to_page_id_list: dict[str, list[str]] = {}

    for page in page_list:
        page_id = get_page_id(page)
        if page_id is None:
            log_warning("Skipped game page without page id during entry date sync.")
            continue

        game_name = get_page_name(page)
        if game_name is None:
            log_warning(f"Skipped game page without Name during entry date sync. page_id={page_id}")
            continue

        normalized_game_name = normalize_game_name_for_entry_date_match(game_name)
        if not normalized_game_name:
            log_warning(f"Skipped game page with empty normalized Name during entry date sync. page_id={page_id}")
            continue

        game_name_to_page_id_list.setdefault(normalized_game_name, []).append(page_id)

    return game_name_to_page_id_list


def update_entry_date(config: NotionConfig, page_id: str, game_name: str, entry_date: str) -> bool:
    """Update one Notion game page's 入库日期 property."""

    url = f"https://api.notion.com/v1/pages/{page_id}"
    body = {
        "properties": {
            ENTRY_DATE_PROPERTY_NAME: {
                "date": {
                    "start": entry_date,
                }
            }
        }
    }
    result = notion_request(config, "PATCH", url, json_body=body)

    if result.ok:
        return True

    log_error(
        "Failed to update game entry date. "
        f"page_id={page_id}, game_name={game_name}, entry_date={entry_date}, "
        f"status={result.status_code}, reason={result.error_message}"
    )
    return False


def run_sync_entry_dates(config: NotionConfig) -> None:
    """Sync CSV entry dates into the Notion game data source by exact game name."""

    if not config.data_source_id:
        log_error("Missing --data-source-id.")
        return

    log_info(
        "Starting sync-entry-dates. "
        f"data_source_id={config.data_source_id}, csv_file_path={config.csv_file_path}"
    )
    row_by_game_name = load_entry_date_csv(config)
    if not row_by_game_name:
        log_warning("No valid CSV entry date rows were loaded. Nothing was updated.")
        return

    page_list = query_all_notion_pages(config, config.data_source_id)
    if page_list is None:
        log_error(
            "Failed to read target game data source. "
            "Check --data-source-id, integration permissions, and the Notion API key."
        )
        return

    log_info(
        "Loaded Notion game pages for entry date sync. "
        f"notion_page_count={len(page_list)}, csv_game_count={len(row_by_game_name)}"
    )
    game_name_to_page_id_list = build_game_name_to_page_id_list(page_list)
    updated_count = 0
    missing_row_list: list[EntryDateRow] = []
    error_count = 0

    for game_name in sorted(row_by_game_name):
        row = row_by_game_name[game_name]
        page_id_list = game_name_to_page_id_list.get(game_name, [])
        if not page_id_list:
            missing_row_list.append(row)
            continue

        for page_id in page_id_list:
            if update_entry_date(config, page_id, game_name, row.entry_date):
                updated_count += 1
                log_info(
                    "Updated game entry date. "
                    f"game_name={game_name}, page_id={page_id}, entry_date={row.entry_date}"
                )
            else:
                error_count += 1

    log_info(
        "Sync entry dates summary: "
        f"csv_game_count={len(row_by_game_name)}, "
        f"notion_page_count={len(page_list)}, "
        f"updated_count={updated_count}, "
        f"missing_count={len(missing_row_list)}, "
        f"error_count={error_count}"
    )
    print_unmatched_entry_date_rows(missing_row_list)


def print_unmatched_entry_date_rows(missing_row_list: list[EntryDateRow]) -> None:
    """Print unmatched CSV rows after the entry date sync summary."""

    if not missing_row_list:
        log_info("No unmatched CSV entry date rows.")
        return

    log_warning("Unmatched CSV entry date rows:")
    for row in missing_row_list:
        log_warning(f"  {format_csv_row(row)}")


def parse_rebuild_game_page(page: dict[str, Any]) -> Optional[RebuildGamePage]:
    """Parse one game table page for derived statistic rebuild."""

    page_id = get_page_id(page)
    if page_id is None:
        log_warning("Skipped game page without page id during derived rebuild.")
        return None

    property_map = page.get("properties", {})
    if not isinstance(property_map, dict):
        log_warning(f"Skipped game page without properties during derived rebuild. page_id={page_id}")
        return None

    app_id = get_page_app_id(page)
    if app_id is None:
        log_warning(f"Skipped game page without valid AppID during derived rebuild. page_id={page_id}")
        return None

    return RebuildGamePage(
        page_id=page_id,
        app_id=app_id,
        name=get_notion_title(property_map, "Name") or f"Unknown Game {app_id}",
        buy_year=get_notion_formula_year_text(property_map, BUY_YEAR_PROPERTY_NAME),
        buy_month=get_notion_formula_month_text(property_map, BUY_MONTH_PROPERTY_NAME),
        complete_year=get_notion_formula_year_text(property_map, COMPLETE_YEAR_PROPERTY_NAME),
        complete_month=get_notion_formula_month_text(property_map, COMPLETE_MONTH_PROPERTY_NAME),
        full_achievement_year=get_notion_formula_year_text(property_map, FULL_ACHIEVEMENT_YEAR_PROPERTY_NAME),
        full_achievement_month=get_notion_formula_month_text(property_map, FULL_ACHIEVEMENT_MONTH_PROPERTY_NAME),
    )


def build_rebuild_game_index(game_page_list: list[dict[str, Any]]) -> RebuildGameIndex:
    """Build an AppID keyed game index for derived statistic rebuild."""

    app_id_to_game_page: dict[int, RebuildGamePage] = {}
    duplicate_app_id_set: set[int] = set()

    for page in game_page_list:
        game_page = parse_rebuild_game_page(page)
        if game_page is None:
            continue

        existing_game_page = app_id_to_game_page.get(game_page.app_id)
        if existing_game_page is not None:
            duplicate_app_id_set.add(game_page.app_id)
            log_error(
                "Duplicate AppID found in game table during derived rebuild. "
                f"app_id={game_page.app_id}, first_page_id={existing_game_page.page_id}, "
                f"duplicate_page_id={game_page.page_id}."
            )
            continue

        app_id_to_game_page[game_page.app_id] = game_page

    for app_id in duplicate_app_id_set:
        app_id_to_game_page.pop(app_id, None)

    return RebuildGameIndex(
        app_id_to_game_page=app_id_to_game_page,
        duplicate_app_id_set=duplicate_app_id_set,
    )


def parse_playtime_record(
    page: dict[str, Any],
    game_index: RebuildGameIndex,
) -> Optional[PlaytimeRecord]:
    """Parse one playtime history page for derived statistic rebuild."""

    page_id = get_page_id(page)
    if page_id is None:
        log_warning("Skipped playtime page without page id during derived rebuild.")
        return None

    app_id = get_page_app_id(page)
    if app_id is None:
        log_warning(f"Skipped playtime page without valid AppID during derived rebuild. page_id={page_id}")
        return None

    if app_id in game_index.duplicate_app_id_set:
        log_error(
            "Skipped playtime page because matching game AppID is duplicated. "
            f"app_id={app_id}, playtime_page_id={page_id}"
        )
        return None

    game_page = game_index.app_id_to_game_page.get(app_id)
    if game_page is None:
        log_warning(
            "Skipped playtime page because no matching game page exists. "
            f"app_id={app_id}, playtime_page_id={page_id}"
        )
        return None

    property_map = page.get("properties", {})
    if not isinstance(property_map, dict):
        log_warning(f"Skipped playtime page without properties during derived rebuild. page_id={page_id}")
        return None

    delta_number = get_notion_number(property_map, "DeltaMinutes")
    delta_minutes = int(delta_number) if delta_number is not None else None

    return PlaytimeRecord(
        page_id=page_id,
        app_id=app_id,
        record_date=get_notion_date(property_map, "Date"),
        delta_minutes=delta_minutes,
        game_page=game_page,
    )


def parse_playtime_record_list(
    playtime_page_list: list[dict[str, Any]],
    game_index: RebuildGameIndex,
) -> list[PlaytimeRecord]:
    """Parse all usable playtime pages for derived statistic rebuild."""

    playtime_record_list: list[PlaytimeRecord] = []
    for page in playtime_page_list:
        playtime_record = parse_playtime_record(page, game_index)
        if playtime_record is not None:
            playtime_record_list.append(playtime_record)
    return playtime_record_list


def is_positive_dated_playtime_record(playtime_record: PlaytimeRecord) -> bool:
    """Return true when a playtime record can contribute to derived statistics."""

    return (
        playtime_record.record_date is not None
        and playtime_record.delta_minutes is not None
        and playtime_record.delta_minutes > 0
    )


def build_period_aggregate_map(playtime_record_list: list[PlaytimeRecord]) -> dict[str, PeriodAggregate]:
    """Aggregate valid playtime records into yearly and monthly game periods."""

    period_id_to_aggregate: dict[str, PeriodAggregate] = {}

    for playtime_record in playtime_record_list:
        if not is_positive_dated_playtime_record(playtime_record):
            continue

        record_date = playtime_record.record_date
        delta_minutes = playtime_record.delta_minutes
        if record_date is None or delta_minutes is None:
            continue

        add_period_aggregate(period_id_to_aggregate, playtime_record, delta_minutes, record_date, PERIOD_TYPE_YEAR)
        add_period_aggregate(period_id_to_aggregate, playtime_record, delta_minutes, record_date, PERIOD_TYPE_MONTH)

    return period_id_to_aggregate


def add_period_aggregate(
    period_id_to_aggregate: dict[str, PeriodAggregate],
    playtime_record: PlaytimeRecord,
    delta_minutes: int,
    record_date: date,
    period_type: str,
) -> None:
    """Add one playtime record delta to one period aggregate."""

    year_text = f"{record_date.year:04d}"
    if period_type == PERIOD_TYPE_YEAR:
        period_text = year_text
        month: Optional[int] = None
    else:
        period_text = f"{record_date.year:04d}-{record_date.month:02d}"
        month = record_date.month

    period_id = f"{period_text}_{playtime_record.app_id}"
    period_aggregate = period_id_to_aggregate.get(period_id)
    if period_aggregate is None:
        period_aggregate = PeriodAggregate(
            period_id=period_id,
            period_text=period_text,
            period_type=period_type,
            year=record_date.year,
            month=month,
            app_id=playtime_record.app_id,
            game_name=playtime_record.game_page.name,
            game_page_id=playtime_record.game_page.page_id,
            playtime_minutes=0,
            playtime_page_id_list=[],
        )
        period_id_to_aggregate[period_id] = period_aggregate

    period_aggregate.playtime_minutes += delta_minutes
    if period_aggregate.playtime_page_id_list is not None:
        period_aggregate.playtime_page_id_list.append(playtime_record.page_id)


def parse_period_stat_page(page: dict[str, Any]) -> Optional[PeriodStatPage]:
    """Parse one period statistic page."""

    page_id = get_page_id(page)
    if page_id is None:
        log_warning("Skipped period statistic page without page id during derived rebuild.")
        return None

    property_map = page.get("properties", {})
    if not isinstance(property_map, dict):
        log_warning(f"Skipped period statistic page without properties. page_id={page_id}")
        return None

    period_id = get_notion_rich_text(property_map, "PeriodID")
    if not period_id:
        log_warning(f"Skipped period statistic page without PeriodID. page_id={page_id}")
        return None

    return PeriodStatPage(
        page_id=page_id,
        period_id=period_id,
        period_text=get_notion_select_name(property_map, "Period"),
        period_type=get_notion_select_name(property_map, "Type"),
        playtime_minutes=int(get_notion_number(property_map, "PlayTimeMinutes") or 0),
    )


def build_period_stat_page_index(
    period_page_list: list[dict[str, Any]],
) -> tuple[dict[str, PeriodStatPage], set[str]]:
    """Build a PeriodID keyed index from existing period statistic pages."""

    period_id_to_page: dict[str, PeriodStatPage] = {}
    duplicate_period_id_set: set[str] = set()

    for page in period_page_list:
        period_stat_page = parse_period_stat_page(page)
        if period_stat_page is None:
            continue

        if period_stat_page.period_id in period_id_to_page:
            duplicate_period_id_set.add(period_stat_page.period_id)
            log_error(
                "Duplicate PeriodID found during derived rebuild. "
                f"period_id={period_stat_page.period_id}."
            )
            continue

        period_id_to_page[period_stat_page.period_id] = period_stat_page

    for period_id in duplicate_period_id_set:
        period_id_to_page.pop(period_id, None)

    return period_id_to_page, duplicate_period_id_set


def parse_summary_page_key(page: dict[str, Any]) -> Optional[tuple[str, str]]:
    """Parse one summary page key as a Type and Period pair."""

    page_id = str(page.get("id", "unknown"))
    property_map = page.get("properties", {})
    if not isinstance(property_map, dict):
        log_warning(f"Skipped summary page without properties. page_id={page_id}")
        return None

    period_text = get_notion_title(property_map, "Period")
    period_type = get_notion_select_name(property_map, "Type")
    if not period_text or not period_type:
        log_warning(f"Skipped summary page without Period or Type. page_id={page_id}")
        return None

    return normalize_summary_key(period_type, period_text, f"summary_page_id={page_id}")


def normalize_summary_key(period_type: str, period_text: str, context: str) -> Optional[tuple[str, str]]:
    """Validate and normalize a summary Type and Period pair."""

    period_type = period_type.strip()
    period_text = period_text.strip()

    if period_type == PERIOD_TYPE_YEAR and len(period_text) == 4 and period_text.isdigit():
        return period_type, period_text

    if (
        period_type == PERIOD_TYPE_MONTH
        and len(period_text) == 7
        and period_text[4] == "-"
        and period_text[:4].isdigit()
        and period_text[5:].isdigit()
        and 1 <= int(period_text[5:]) <= 12
    ):
        return period_type, period_text

    log_warning(
        "Skipped invalid summary period. "
        f"context={context}, period={period_text}, type={period_type}"
    )
    return None


def build_summary_page_info_index(
    summary_page_list: list[dict[str, Any]],
) -> tuple[dict[tuple[str, str], SummaryPageInfo], set[tuple[str, str]]]:
    """Build a summary page index with current numeric values."""

    summary_page_info_index: dict[tuple[str, str], SummaryPageInfo] = {}
    duplicate_key_set: set[tuple[str, str]] = set()

    for page in summary_page_list:
        summary_key = parse_summary_page_key(page)
        if summary_key is None:
            continue

        page_id = get_page_id(page)
        if page_id is None:
            log_warning(f"Skipped summary page without page id. key={summary_key}")
            continue

        if summary_key in summary_page_info_index:
            duplicate_key_set.add(summary_key)
            log_error(
                "Duplicate summary row found during derived rebuild. "
                f"period={summary_key[1]}, type={summary_key[0]}."
            )
            continue

        summary_page_info_index[summary_key] = SummaryPageInfo(
            page_id=page_id,
            summary_count=parse_summary_count_from_page(page),
        )

    for duplicate_key in duplicate_key_set:
        summary_page_info_index.pop(duplicate_key, None)

    return summary_page_info_index, duplicate_key_set


def parse_summary_count_from_page(page: dict[str, Any]) -> SummaryCount:
    """Parse current numeric summary count fields from one summary page."""

    property_map = page.get("properties", {})
    if not isinstance(property_map, dict):
        return SummaryCount()

    return SummaryCount(
        new_game_count=int(get_notion_number(property_map, NEW_GAME_NUM_PROPERTY_NAME) or 0),
        complete_game_count=int(get_notion_number(property_map, COMPLETE_GAME_NUM_PROPERTY_NAME) or 0),
        full_achievement_game_count=int(get_notion_number(property_map, FULL_ACHIEVEMENT_GAME_NUM_PROPERTY_NAME) or 0),
        played_game_count=int(get_notion_number(property_map, PLAYED_GAME_NUM_PROPERTY_NAME) or 0),
        total_playtime_minutes=int(get_notion_number(property_map, TOTAL_PLAYTIME_MINUTES_PROPERTY_NAME) or 0),
    )


def ensure_summary_page(
    config: NotionConfig,
    summary_page_info_index: dict[tuple[str, str], SummaryPageInfo],
    duplicate_summary_key_set: set[tuple[str, str]],
    summary_key: tuple[str, str],
    stats: RebuildStats,
) -> Optional[str]:
    """Find or create a summary page for one Type and Period key."""

    if summary_key in duplicate_summary_key_set:
        stats.error_count += 1
        log_error(
            "Cannot ensure summary page because duplicate summary rows exist. "
            f"period={summary_key[1]}, type={summary_key[0]}."
        )
        return None

    existing_summary_page = summary_page_info_index.get(summary_key)
    if existing_summary_page is not None:
        return existing_summary_page.page_id

    if not config.summary_data_source_id:
        stats.error_count += 1
        log_error("Missing --summary-data-source-id.")
        return None

    property_map = {
        "Period": build_title_property(summary_key[1]),
        "Type": build_select_property(summary_key[0]),
    }
    context = f"summary_period={summary_key[1]}, type={summary_key[0]}"

    if config.dry_run:
        stats.created_summary_count += 1
        dry_run_page_id = f"dry-run-summary:{summary_key[0]}:{summary_key[1]}"
        summary_page_info_index[summary_key] = SummaryPageInfo(
            page_id=dry_run_page_id,
            summary_count=SummaryCount(),
        )
        log_info(f"[DRY-RUN] Would create summary page. {context}")
        return dry_run_page_id

    page_id = create_notion_page(config, config.summary_data_source_id, property_map, context)
    if page_id is None:
        stats.error_count += 1
        return None

    stats.created_summary_count += 1
    summary_page_info_index[summary_key] = SummaryPageInfo(page_id=page_id, summary_count=SummaryCount())
    log_info(f"Created summary page. {context}, page_id={page_id}")
    return page_id


def build_period_stat_properties(
    period_aggregate: PeriodAggregate,
    summary_page_id: Optional[str],
) -> dict[str, Any]:
    """Build Notion properties for one period statistic page."""

    property_map = {
        "Name": build_title_property(f"{period_aggregate.period_text}_{period_aggregate.game_name}"),
        "PeriodID": build_rich_text_property(period_aggregate.period_id),
        "Period": build_select_property(period_aggregate.period_text),
        "Type": build_select_property(period_aggregate.period_type),
        "Year": build_select_property(f"{period_aggregate.year:04d}"),
        "Month": build_optional_select_property(
            str(period_aggregate.month) if period_aggregate.month is not None else None
        ),
        "AppID": {"number": period_aggregate.app_id},
        "PlayTimeMinutes": {"number": period_aggregate.playtime_minutes},
        GAME_LOG_RELATION_PROPERTY_NAME: build_relation_property(period_aggregate.game_page_id),
    }

    if summary_page_id:
        property_map[SUMMARY_RELATION_PROPERTY_NAME] = build_relation_property(summary_page_id)

    return property_map


def upsert_period_stat_pages(
    config: NotionConfig,
    period_id_to_aggregate: dict[str, PeriodAggregate],
    period_id_to_page: dict[str, PeriodStatPage],
    duplicate_period_id_set: set[str],
    summary_page_info_index: dict[tuple[str, str], SummaryPageInfo],
    duplicate_summary_key_set: set[tuple[str, str]],
    stats: RebuildStats,
) -> None:
    """Create or update period statistic pages from aggregate values."""

    if not config.period_data_source_id:
        stats.error_count += 1
        log_error("Missing --period-data-source-id.")
        return

    for period_id in sorted(period_id_to_aggregate):
        period_aggregate = period_id_to_aggregate[period_id]
        if period_id in duplicate_period_id_set:
            stats.error_count += 1
            log_error(f"Skipped period stat upsert because PeriodID is duplicated. period_id={period_id}")
            continue

        summary_key = (period_aggregate.period_type, period_aggregate.period_text)
        summary_page_id = ensure_summary_page(
            config,
            summary_page_info_index,
            duplicate_summary_key_set,
            summary_key,
            stats,
        )
        period_aggregate.summary_page_id = summary_page_id
        property_map = build_period_stat_properties(period_aggregate, summary_page_id)
        existing_period_page = period_id_to_page.get(period_id)

        if existing_period_page is None:
            if config.dry_run:
                stats.created_period_count += 1
                period_aggregate.period_page_id = f"dry-run-period:{period_id}"
                log_info(
                    "[DRY-RUN] Would create period stat page. "
                    f"period_id={period_id}, playtime_minutes={period_aggregate.playtime_minutes}"
                )
                continue

            page_id = create_notion_page(
                config,
                config.period_data_source_id,
                property_map,
                f"period_id={period_id}",
            )
            if page_id is None:
                stats.error_count += 1
                continue

            period_aggregate.period_page_id = page_id
            period_id_to_page[period_id] = PeriodStatPage(
                page_id=page_id,
                period_id=period_id,
                period_text=period_aggregate.period_text,
                period_type=period_aggregate.period_type,
                playtime_minutes=period_aggregate.playtime_minutes,
            )
            stats.created_period_count += 1
            log_info(f"Created period stat page. period_id={period_id}, page_id={page_id}")
            continue

        period_aggregate.period_page_id = existing_period_page.page_id
        if config.dry_run:
            stats.updated_period_count += 1
            log_info(
                "[DRY-RUN] Would update period stat page. "
                f"period_id={period_id}, page_id={existing_period_page.page_id}, "
                f"playtime_minutes={period_aggregate.playtime_minutes}"
            )
            continue

        if update_notion_page(config, existing_period_page.page_id, property_map, f"period_id={period_id}"):
            stats.updated_period_count += 1
            log_info(
                "Updated period stat page. "
                f"period_id={period_id}, page_id={existing_period_page.page_id}, "
                f"playtime_minutes={period_aggregate.playtime_minutes}"
            )
        else:
            stats.error_count += 1


def archive_extra_period_stat_pages(
    config: NotionConfig,
    period_id_to_aggregate: dict[str, PeriodAggregate],
    period_id_to_page: dict[str, PeriodStatPage],
    stats: RebuildStats,
) -> None:
    """Archive period statistic pages that are no longer derived from playtime records."""

    for period_id in sorted(period_id_to_page):
        if period_id in period_id_to_aggregate:
            continue

        period_page = period_id_to_page[period_id]
        if config.keep_extra_period_stats:
            log_warning(
                "Extra period stat page was kept because --keep-extra-period-stats is enabled. "
                f"period_id={period_id}, page_id={period_page.page_id}"
            )
            continue

        if config.dry_run:
            stats.archived_period_count += 1
            log_info(
                "[DRY-RUN] Would archive extra period stat page. "
                f"period_id={period_id}, page_id={period_page.page_id}"
            )
            continue

        if archive_notion_page(config, period_page.page_id, f"extra_period_id={period_id}"):
            stats.archived_period_count += 1
            log_info(f"Archived extra period stat page. period_id={period_id}, page_id={period_page.page_id}")
        else:
            stats.error_count += 1


def build_playtime_derived_properties(
    playtime_record: PlaytimeRecord,
    period_id_to_aggregate: dict[str, PeriodAggregate],
) -> dict[str, Any]:
    """Build repaired relation properties for one playtime record."""

    property_map: dict[str, Any] = {
        GAME_LOG_RELATION_PROPERTY_NAME: build_relation_property(playtime_record.game_page.page_id),
    }

    if not is_positive_dated_playtime_record(playtime_record):
        property_map[PERIOD_RELATION_PROPERTY_NAME] = build_relation_list_property([])
        return property_map

    record_date = playtime_record.record_date
    if record_date is None:
        property_map[PERIOD_RELATION_PROPERTY_NAME] = build_relation_list_property([])
        return property_map

    year_period_id = f"{record_date.year:04d}_{playtime_record.app_id}"
    month_period_id = f"{record_date.year:04d}-{record_date.month:02d}_{playtime_record.app_id}"
    period_page_id_list: list[str] = []
    for period_id in [year_period_id, month_period_id]:
        period_aggregate = period_id_to_aggregate.get(period_id)
        if period_aggregate is not None and period_aggregate.period_page_id:
            period_page_id_list.append(period_aggregate.period_page_id)

    if len(period_page_id_list) == 2:
        property_map[PERIOD_RELATION_PROPERTY_NAME] = build_relation_list_property(period_page_id_list)
    else:
        log_warning(
            "Skipped precise PeriodRelation repair because period pages are unavailable. "
            f"playtime_page_id={playtime_record.page_id}, app_id={playtime_record.app_id}"
        )

    return property_map


def update_playtime_record_relations(
    config: NotionConfig,
    playtime_record_list: list[PlaytimeRecord],
    period_id_to_aggregate: dict[str, PeriodAggregate],
    stats: RebuildStats,
) -> None:
    """Repair GameLogRelation and PeriodRelation for playtime records."""

    for playtime_record in playtime_record_list:
        property_map = build_playtime_derived_properties(playtime_record, period_id_to_aggregate)
        context = f"playtime_page_id={playtime_record.page_id}, app_id={playtime_record.app_id}"

        if config.dry_run:
            stats.updated_playtime_count += 1
            log_info(f"[DRY-RUN] Would update playtime derived relations. {context}")
            continue

        if update_notion_page(config, playtime_record.page_id, property_map, context):
            stats.updated_playtime_count += 1
            log_info(f"Updated playtime derived relations. {context}")
        else:
            stats.error_count += 1


def build_game_period_value_index(
    game_index: RebuildGameIndex,
    period_id_to_aggregate: dict[str, PeriodAggregate],
) -> dict[int, dict[str, list[str]]]:
    """Build game table derived relation and grouping values."""

    game_period_value_index: dict[int, dict[str, list[str]]] = {}
    for app_id in game_index.app_id_to_game_page:
        game_period_value_index[app_id] = {
            "year_summary_page_id_list": [],
            "month_summary_page_id_list": [],
            "played_year_list": [],
            "played_month_list": [],
        }

    for period_aggregate in period_id_to_aggregate.values():
        value_map = game_period_value_index.setdefault(
            period_aggregate.app_id,
            {
                "year_summary_page_id_list": [],
                "month_summary_page_id_list": [],
                "played_year_list": [],
                "played_month_list": [],
            },
        )
        if period_aggregate.period_type == PERIOD_TYPE_YEAR:
            append_unique(value_map["played_year_list"], period_aggregate.period_text)
            if period_aggregate.summary_page_id:
                append_unique(value_map["year_summary_page_id_list"], period_aggregate.summary_page_id)
        elif period_aggregate.period_type == PERIOD_TYPE_MONTH:
            append_unique(value_map["played_month_list"], period_aggregate.period_text)
            if period_aggregate.summary_page_id:
                append_unique(value_map["month_summary_page_id_list"], period_aggregate.summary_page_id)

    for value_map in game_period_value_index.values():
        value_map["played_year_list"].sort()
        value_map["played_month_list"].sort()

    return game_period_value_index


def append_unique(text_list: list[str], text: str) -> None:
    """Append text to a list once."""

    if text and text not in text_list:
        text_list.append(text)


def update_game_derived_fields(
    config: NotionConfig,
    game_index: RebuildGameIndex,
    period_id_to_aggregate: dict[str, PeriodAggregate],
    stats: RebuildStats,
) -> None:
    """Overwrite game table derived relation and grouping fields."""

    game_period_value_index = build_game_period_value_index(game_index, period_id_to_aggregate)

    for app_id in sorted(game_index.app_id_to_game_page):
        game_page = game_index.app_id_to_game_page[app_id]
        value_map = game_period_value_index.get(app_id, {})
        property_map = {
            YEAR_SUMMARY_RELATION_PROPERTY_NAME: build_relation_list_property(
                value_map.get("year_summary_page_id_list", [])
            ),
            MONTH_SUMMARY_RELATION_PROPERTY_NAME: build_relation_list_property(
                value_map.get("month_summary_page_id_list", [])
            ),
            PLAYED_YEAR_PROPERTY_NAME: build_multi_select_property(value_map.get("played_year_list", [])),
            PLAYED_MONTH_PROPERTY_NAME: build_multi_select_property(value_map.get("played_month_list", [])),
        }
        context = f"game_page_id={game_page.page_id}, app_id={app_id}, name={game_page.name}"

        if config.dry_run:
            stats.updated_game_count += 1
            log_info(f"[DRY-RUN] Would overwrite game derived fields. {context}")
            continue

        if update_notion_page(config, game_page.page_id, property_map, context):
            stats.updated_game_count += 1
            log_info(f"Overwrote game derived fields. {context}")
        else:
            stats.error_count += 1


def build_summary_count_map(
    game_index: RebuildGameIndex,
    period_id_to_aggregate: dict[str, PeriodAggregate],
) -> dict[tuple[str, str], SummaryCount]:
    """Build full summary count values from games and rebuilt period aggregates."""

    summary_count_map: dict[tuple[str, str], SummaryCount] = {}

    for game_page in game_index.app_id_to_game_page.values():
        add_game_field_summary_count(
            summary_count_map,
            game_page.buy_year,
            game_page.buy_month,
            NEW_GAME_NUM_PROPERTY_NAME,
        )
        add_game_field_summary_count(
            summary_count_map,
            game_page.complete_year,
            game_page.complete_month,
            COMPLETE_GAME_NUM_PROPERTY_NAME,
        )
        add_game_field_summary_count(
            summary_count_map,
            game_page.full_achievement_year,
            game_page.full_achievement_month,
            FULL_ACHIEVEMENT_GAME_NUM_PROPERTY_NAME,
        )

    for period_aggregate in period_id_to_aggregate.values():
        summary_key = (period_aggregate.period_type, period_aggregate.period_text)
        summary_count = get_or_create_summary_count(summary_count_map, summary_key)
        summary_count.played_game_count += 1
        summary_count.total_playtime_minutes += period_aggregate.playtime_minutes

    return summary_count_map


def add_game_field_summary_count(
    summary_count_map: dict[tuple[str, str], SummaryCount],
    year_text: Optional[str],
    month_text: Optional[str],
    property_name: str,
) -> None:
    """Add one game field count to year and month summary buckets."""

    if year_text:
        summary_count = get_or_create_summary_count(summary_count_map, (PERIOD_TYPE_YEAR, year_text))
        increment_summary_count(summary_count, property_name)

    if month_text:
        summary_count = get_or_create_summary_count(summary_count_map, (PERIOD_TYPE_MONTH, month_text))
        increment_summary_count(summary_count, property_name)


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


def increment_summary_count(summary_count: SummaryCount, property_name: str) -> None:
    """Increment one named summary count field."""

    if property_name == NEW_GAME_NUM_PROPERTY_NAME:
        summary_count.new_game_count += 1
    elif property_name == COMPLETE_GAME_NUM_PROPERTY_NAME:
        summary_count.complete_game_count += 1
    elif property_name == FULL_ACHIEVEMENT_GAME_NUM_PROPERTY_NAME:
        summary_count.full_achievement_game_count += 1


def build_summary_count_properties(summary_count: SummaryCount) -> dict[str, Any]:
    """Build Notion properties for full recomputed summary counts."""

    return {
        NEW_GAME_NUM_PROPERTY_NAME: {"number": summary_count.new_game_count},
        COMPLETE_GAME_NUM_PROPERTY_NAME: {"number": summary_count.complete_game_count},
        FULL_ACHIEVEMENT_GAME_NUM_PROPERTY_NAME: {"number": summary_count.full_achievement_game_count},
        PLAYED_GAME_NUM_PROPERTY_NAME: {"number": summary_count.played_game_count},
        TOTAL_PLAYTIME_MINUTES_PROPERTY_NAME: {"number": summary_count.total_playtime_minutes},
    }


def summary_count_equals(left: SummaryCount, right: SummaryCount) -> bool:
    """Return true when two SummaryCount values are equal."""

    return (
        left.new_game_count == right.new_game_count
        and left.complete_game_count == right.complete_game_count
        and left.full_achievement_game_count == right.full_achievement_game_count
        and left.played_game_count == right.played_game_count
        and left.total_playtime_minutes == right.total_playtime_minutes
    )


def update_summary_count_pages(
    config: NotionConfig,
    summary_count_map: dict[tuple[str, str], SummaryCount],
    summary_page_info_index: dict[tuple[str, str], SummaryPageInfo],
    duplicate_summary_key_set: set[tuple[str, str]],
    stats: RebuildStats,
) -> None:
    """Ensure summary pages exist and update numeric summary counts."""

    target_key_set = set(summary_count_map) | set(summary_page_info_index)
    for summary_key in sorted(target_key_set, key=sort_summary_key):
        if summary_key in duplicate_summary_key_set:
            stats.skipped_count += 1
            log_error(
                "Skipped summary count update because duplicate summary rows exist. "
                f"period={summary_key[1]}, type={summary_key[0]}."
            )
            continue

        summary_count = summary_count_map.get(summary_key, SummaryCount())
        summary_page_id = ensure_summary_page(
            config,
            summary_page_info_index,
            duplicate_summary_key_set,
            summary_key,
            stats,
        )
        if summary_page_id is None:
            continue

        current_summary_count = summary_page_info_index[summary_key].summary_count
        if summary_count_equals(current_summary_count, summary_count):
            stats.unchanged_summary_count += 1
            log_info(
                "Summary count unchanged. "
                f"period={summary_key[1]}, type={summary_key[0]}"
            )
            continue

        property_map = build_summary_count_properties(summary_count)
        context = f"summary_period={summary_key[1]}, type={summary_key[0]}"
        if config.dry_run:
            stats.updated_summary_count += 1
            log_info(f"[DRY-RUN] Would update summary count page. {context}")
            continue

        if update_notion_page(config, summary_page_id, property_map, context):
            stats.updated_summary_count += 1
            summary_page_info_index[summary_key].summary_count = summary_count
            log_info(f"Updated summary count page. {context}")
        else:
            stats.error_count += 1


def sort_summary_key(summary_key: tuple[str, str]) -> tuple[str, int]:
    """Sort summary keys by Period text and Year before Month."""

    period_type = summary_key[0]
    period_text = summary_key[1]
    type_order = 0 if period_type == PERIOD_TYPE_YEAR else 1
    return period_text, type_order


def run_rebuild_derived_stats(config: NotionConfig) -> None:
    """Rebuild derived period statistics, relations, and summary counts."""

    if (
        not config.game_data_source_id
        or not config.playtime_data_source_id
        or not config.period_data_source_id
        or not config.summary_data_source_id
    ):
        log_error(
            "Missing required data source id. "
            "Need --game-data-source-id, --playtime-data-source-id, "
            "--period-data-source-id, and --summary-data-source-id."
        )
        return

    log_info(
        "Starting rebuild-derived-stats. "
        f"dry_run={config.dry_run}, keep_extra_period_stats={config.keep_extra_period_stats}"
    )

    game_page_list = query_all_notion_pages(config, config.game_data_source_id)
    playtime_page_list = query_all_notion_pages(config, config.playtime_data_source_id)
    period_page_list = query_all_notion_pages(config, config.period_data_source_id)
    summary_page_list = query_all_notion_pages(config, config.summary_data_source_id)
    if (
        game_page_list is None
        or playtime_page_list is None
        or period_page_list is None
        or summary_page_list is None
    ):
        log_error("Failed to load all required Notion data sources. Rebuild was not run.")
        return

    stats = RebuildStats()
    game_index = build_rebuild_game_index(game_page_list)
    playtime_record_list = parse_playtime_record_list(playtime_page_list, game_index)
    period_id_to_aggregate = build_period_aggregate_map(playtime_record_list)
    period_id_to_page, duplicate_period_id_set = build_period_stat_page_index(period_page_list)
    summary_page_info_index, duplicate_summary_key_set = build_summary_page_info_index(summary_page_list)

    log_info(
        "Loaded rebuild source data. "
        f"game_page_count={len(game_page_list)}, "
        f"playtime_page_count={len(playtime_page_list)}, "
        f"period_page_count={len(period_page_list)}, "
        f"summary_page_count={len(summary_page_list)}, "
        f"parsed_playtime_record_count={len(playtime_record_list)}, "
        f"period_aggregate_count={len(period_id_to_aggregate)}"
    )

    upsert_period_stat_pages(
        config,
        period_id_to_aggregate,
        period_id_to_page,
        duplicate_period_id_set,
        summary_page_info_index,
        duplicate_summary_key_set,
        stats,
    )
    archive_extra_period_stat_pages(config, period_id_to_aggregate, period_id_to_page, stats)
    update_playtime_record_relations(config, playtime_record_list, period_id_to_aggregate, stats)
    update_game_derived_fields(config, game_index, period_id_to_aggregate, stats)
    summary_count_map = build_summary_count_map(game_index, period_id_to_aggregate)
    update_summary_count_pages(
        config,
        summary_count_map,
        summary_page_info_index,
        duplicate_summary_key_set,
        stats,
    )

    log_info(
        "Rebuild derived stats summary: "
        f"created_period_count={stats.created_period_count}, "
        f"updated_period_count={stats.updated_period_count}, "
        f"archived_period_count={stats.archived_period_count}, "
        f"updated_playtime_count={stats.updated_playtime_count}, "
        f"updated_game_count={stats.updated_game_count}, "
        f"created_summary_count={stats.created_summary_count}, "
        f"updated_summary_count={stats.updated_summary_count}, "
        f"unchanged_summary_count={stats.unchanged_summary_count}, "
        f"skipped_count={stats.skipped_count}, "
        f"error_count={stats.error_count}"
    )


def load_notion_config(argument: argparse.Namespace) -> Optional[NotionConfig]:
    """Load Notion configuration from arguments and environment variables."""

    notion_api_key = argument.notion_api_key or os.environ.get("NOTION_API_KEY")
    if not notion_api_key and argument.command:
        log_error(
            "Missing Notion API key. Set NOTION_API_KEY in the environment "
            "or pass --notion-api-key."
        )
        return None

    notion_version = (
        argument.notion_version
        or os.environ.get("NOTION_VERSION")
        or NOTION_VERSION_DEFAULT
    )
    return NotionConfig(
        notion_api_key=notion_api_key or "",
        notion_version=notion_version,
        command=argument.command,
        game_data_source_id=getattr(argument, "game_data_source_id", None),
        playtime_data_source_id=getattr(argument, "playtime_data_source_id", None),
        period_data_source_id=getattr(argument, "period_data_source_id", None),
        summary_data_source_id=getattr(argument, "summary_data_source_id", None),
        data_source_id=getattr(argument, "data_source_id", None),
        template_id=getattr(argument, "template_id", None),
        erase_content=bool(getattr(argument, "erase_content", False)),
        dry_run=bool(getattr(argument, "dry_run", False)),
        keep_extra_period_stats=bool(getattr(argument, "keep_extra_period_stats", False)),
        csv_file_path=getattr(argument, "csv_file_path", None),
        game_name_column=getattr(argument, "game_name_column", DEFAULT_GAME_NAME_COLUMN),
        entry_date_column=getattr(argument, "entry_date_column", DEFAULT_ENTRY_DATE_COLUMN),
    )


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the command line parser for Notion maintenance tools."""

    common_parser = argparse.ArgumentParser(add_help=False)
    common_parser.add_argument("--notion-api-key", default=None, help="Override NOTION_API_KEY.")
    common_parser.add_argument(
        "--notion-version",
        default=None,
        help=f"Override NOTION_VERSION. Default: {NOTION_VERSION_DEFAULT}.",
    )

    parser = argparse.ArgumentParser(description="Notion maintenance tools for steam_sync.")
    subparser = parser.add_subparsers(dest="command")

    repair_parser = subparser.add_parser(
        "repair-relations",
        parents=[common_parser],
        help="Repair playtime GameLogRelation values by matching AppID with game pages.",
    )
    repair_parser.add_argument("--game-data-source-id", required=True)
    repair_parser.add_argument("--playtime-data-source-id", required=True)

    template_parser = subparser.add_parser(
        "apply-template",
        parents=[common_parser],
        help="Apply one Notion database page template to every page in a data source.",
    )
    template_parser.add_argument("--data-source-id", required=True)
    template_parser.add_argument("--template-id", required=True)
    template_parser.add_argument(
        "--erase-content",
        action="store_true",
        help="Delete existing page content before applying the template.",
    )

    entry_date_parser = subparser.add_parser(
        "sync-entry-dates",
        parents=[common_parser],
        help="Sync CSV entry dates to game pages by exact Name matching.",
    )
    entry_date_parser.add_argument("--data-source-id", required=True)
    entry_date_parser.add_argument("--csv-file-path", required=True)
    entry_date_parser.add_argument("--game-name-column", default=DEFAULT_GAME_NAME_COLUMN)
    entry_date_parser.add_argument("--entry-date-column", default=DEFAULT_ENTRY_DATE_COLUMN)

    rebuild_parser = subparser.add_parser(
        "rebuild-derived-stats",
        parents=[common_parser],
        help="Rebuild derived period stats, game grouping fields, and summary counts.",
    )
    rebuild_parser.add_argument("--game-data-source-id", required=True)
    rebuild_parser.add_argument("--playtime-data-source-id", required=True)
    rebuild_parser.add_argument("--period-data-source-id", required=True)
    rebuild_parser.add_argument("--summary-data-source-id", required=True)
    rebuild_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned changes without writing to Notion.",
    )
    rebuild_parser.add_argument(
        "--keep-extra-period-stats",
        action="store_true",
        help="Report extra period stat pages instead of archiving them.",
    )

    return parser


def run_command(argument: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Run the selected command."""

    config = load_notion_config(argument)
    if config is None:
        return

    if not config.command:
        parser.print_help()
        return

    if config.command == "repair-relations":
        run_repair_relations(config)
        return

    if config.command == "apply-template":
        run_apply_template(config)
        return

    if config.command == "sync-entry-dates":
        run_sync_entry_dates(config)
        return

    if config.command == "rebuild-derived-stats":
        run_rebuild_derived_stats(config)
        return

    log_error(f"Unknown command: {config.command}")


def main(argument_list: Optional[Sequence[str]] = None) -> None:
    """Program entrypoint."""

    parser = build_argument_parser()
    argument = parser.parse_args(argument_list)

    try:
        run_command(argument, parser)
    except Exception as error:
        log_error(f"Unexpected top-level error was caught: {error}")


if __name__ == "__main__":
    main()
