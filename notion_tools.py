"""Small Notion maintenance tools for the Steam sync data sources."""

from __future__ import annotations

import argparse
import csv
import re
import os
import time
from dataclasses import dataclass
from datetime import date
from typing import Any
from typing import Optional
from typing import Sequence

import requests


NOTION_VERSION_DEFAULT = "2025-09-03"
REQUEST_TIMEOUT_SECONDS = 30
MAX_RETRY_COUNT = 3
RETRY_STATUS_CODE_SET = {429, 500, 502, 503, 504}
GAME_LOG_RELATION_PROPERTY_NAME = "GameLogRelation"
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
    data_source_id: Optional[str] = None
    template_id: Optional[str] = None
    erase_content: bool = False
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

    return {
        "relation": [
            {
                "id": game_page_id,
            }
        ]
    }


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
        data_source_id=getattr(argument, "data_source_id", None),
        template_id=getattr(argument, "template_id", None),
        erase_content=bool(getattr(argument, "erase_content", False)),
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
