"""Small Notion maintenance tools for the Steam sync data sources."""

from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass
from typing import Any
from typing import Optional
from typing import Sequence

import requests


NOTION_VERSION_DEFAULT = "2025-09-03"
REQUEST_TIMEOUT_SECONDS = 30
MAX_RETRY_COUNT = 3
RETRY_STATUS_CODE_SET = {429, 500, 502, 503, 504}
GAME_LOG_RELATION_PROPERTY_NAME = "GameLogRelation"


@dataclass
class NotionConfig:
    """Runtime configuration for Notion maintenance tools."""

    notion_api_key: str
    notion_version: str


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


def run_repair_relations(
    config: NotionConfig,
    game_data_source_id: str,
    playtime_data_source_id: str,
) -> None:
    """Repair GameLogRelation values for every matching playtime history page."""

    game_page_list = query_all_notion_pages(config, game_data_source_id)
    if game_page_list is None:
        log_error(
            "Failed to read game data source. "
            "Check --game-data-source-id, integration permissions, and the Notion API key."
        )
        return

    playtime_page_list = query_all_notion_pages(config, playtime_data_source_id)
    if playtime_page_list is None:
        log_error(
            "Failed to read playtime data source. "
            "Check --playtime-data-source-id, integration permissions, and the Notion API key."
        )
        return

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


def run_apply_template(
    config: NotionConfig,
    data_source_id: str,
    template_id: str,
    erase_content: bool,
) -> None:
    """Apply one Notion template to every page in one data source."""

    page_list = query_all_notion_pages(config, data_source_id)
    if page_list is None:
        log_error(
            "Failed to read target data source. "
            "Check --data-source-id, integration permissions, and the Notion API key."
        )
        return

    updated_count = 0
    skipped_count = 0
    error_count = 0

    for page in page_list:
        page_id = get_page_id(page)
        if page_id is None:
            skipped_count += 1
            log_warning("Skipped target page without page id.")
            continue

        if apply_template_to_page(config, page_id, template_id, erase_content):
            updated_count += 1
        else:
            error_count += 1

    log_info(
        "Apply template summary: "
        f"page_count={len(page_list)}, "
        f"updated_count={updated_count}, "
        f"skipped_count={skipped_count}, "
        f"error_count={error_count}, "
        f"erase_content={erase_content}"
    )


def load_notion_config(argument: argparse.Namespace) -> Optional[NotionConfig]:
    """Load Notion configuration from arguments and environment variables."""

    notion_api_key = argument.notion_api_key or os.environ.get("NOTION_API_KEY")
    if not notion_api_key:
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
    return NotionConfig(notion_api_key=notion_api_key, notion_version=notion_version)


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

    return parser


def run_command(argument: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Run the selected command."""

    if not argument.command:
        parser.print_help()
        return

    config = load_notion_config(argument)
    if config is None:
        return

    if argument.command == "repair-relations":
        run_repair_relations(
            config,
            argument.game_data_source_id,
            argument.playtime_data_source_id,
        )
        return

    if argument.command == "apply-template":
        run_apply_template(
            config,
            argument.data_source_id,
            argument.template_id,
            argument.erase_content,
        )
        return

    log_error(f"Unknown command: {argument.command}")


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
