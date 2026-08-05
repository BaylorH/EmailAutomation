"""Pure Google Sheets row DeveloperMetadata dictionary contracts."""

from __future__ import annotations

from email_automation.row_authority import validate_row_id


MARKER_KEY = "sitesift_row_id_v1"
MARKER_VISIBILITY = "DOCUMENT"
ROW_LOCATION_TYPE = "ROW"
ROW_DIMENSION = "ROWS"
MAX_ROW_MARKER_MATCHES = 128


def _reject(message):
    raise ValueError(message)


def _require_row_id(value, *, field_name):
    try:
        return validate_row_id(value)
    except RuntimeError as exc:
        raise ValueError(f"{field_name} must be a canonical row ID") from exc


def _require_nonnegative_integer(value, *, field_name):
    if type(value) is not int or value < 0:
        _reject(f"{field_name} must be a nonnegative integer")
    return value


def _marker_lookup(row_id):
    return {
        "developerMetadataLookup": {
            "metadataKey": MARKER_KEY,
            "metadataValue": row_id,
            "visibility": MARKER_VISIBILITY,
            "locationType": ROW_LOCATION_TYPE,
        }
    }


def _is_exact_marker_filter(value, *, row_id):
    if type(value) is not dict or set(value) != {"developerMetadataLookup"}:
        return False
    lookup = value["developerMetadataLookup"]
    return (
        type(lookup) is dict
        and set(lookup)
        == {
            "metadataKey",
            "metadataValue",
            "visibility",
            "locationType",
        }
        and lookup == _marker_lookup(row_id)["developerMetadataLookup"]
    )


def build_row_marker_create_request(
    *,
    row_id,
    sheet_id,
    provider_row_index,
):
    """Return one exact createDeveloperMetadata batch-update request item."""

    checked_row_id = _require_row_id(row_id, field_name="row_id")
    checked_sheet_id = _require_nonnegative_integer(
        sheet_id,
        field_name="sheet_id",
    )
    checked_index = _require_nonnegative_integer(
        provider_row_index,
        field_name="provider_row_index",
    )
    return {
        "createDeveloperMetadata": {
            "developerMetadata": {
                "metadataKey": MARKER_KEY,
                "metadataValue": checked_row_id,
                "location": {
                    "dimensionRange": {
                        "sheetId": checked_sheet_id,
                        "dimension": ROW_DIMENSION,
                        "startIndex": checked_index,
                        "endIndex": checked_index + 1,
                    }
                },
                "visibility": MARKER_VISIBILITY,
            }
        }
    }


def build_row_marker_search_request(*, row_id):
    """Return one exact DeveloperMetadata search request body."""

    checked_row_id = _require_row_id(row_id, field_name="row_id")
    return {"dataFilters": [_marker_lookup(checked_row_id)]}


def parse_row_developer_metadata(metadata):
    """Validate a direct row DeveloperMetadata object into an observation."""

    if type(metadata) is not dict or set(metadata) != {
        "metadataId",
        "metadataKey",
        "metadataValue",
        "location",
        "visibility",
    }:
        _reject("developer metadata must contain the exact response fields")

    metadata_id = metadata["metadataId"]
    if type(metadata_id) is not int or metadata_id < 1:
        _reject("metadataId must be a positive integer")
    if metadata["metadataKey"] != MARKER_KEY:
        _reject("metadataKey is not the SiteSift row marker key")
    row_id = _require_row_id(
        metadata["metadataValue"],
        field_name="metadataValue",
    )
    if metadata["visibility"] != MARKER_VISIBILITY:
        _reject("visibility must be DOCUMENT")

    location = metadata["location"]
    if type(location) is not dict or set(location) != {
        "locationType",
        "dimensionRange",
    }:
        _reject("location must contain the exact row response fields")
    if location["locationType"] != ROW_LOCATION_TYPE:
        _reject("locationType must be ROW")

    dimension_range = location["dimensionRange"]
    if type(dimension_range) is not dict or set(dimension_range) != {
        "sheetId",
        "dimension",
        "startIndex",
        "endIndex",
    }:
        _reject("dimensionRange must contain the exact row range fields")
    sheet_id = _require_nonnegative_integer(
        dimension_range["sheetId"],
        field_name="sheetId",
    )
    if dimension_range["dimension"] != ROW_DIMENSION:
        _reject("dimensionRange dimension must be ROWS")
    provider_row_index = _require_nonnegative_integer(
        dimension_range["startIndex"],
        field_name="startIndex",
    )
    end_index = _require_nonnegative_integer(
        dimension_range["endIndex"],
        field_name="endIndex",
    )
    if end_index != provider_row_index + 1:
        _reject("dimensionRange must cover exactly one row")

    return {
        "rowId": row_id,
        "sheetId": sheet_id,
        "providerRowIndex": provider_row_index,
        "displayRowNumber": provider_row_index + 1,
        "metadataId": metadata_id,
    }


def parse_row_marker_search_response(response, *, expected_row_id):
    """Validate a DeveloperMetadata search response into canonical matches."""

    checked_row_id = _require_row_id(
        expected_row_id,
        field_name="expected_row_id",
    )
    if type(response) is not dict:
        _reject("search response must be a dictionary")
    if response == {}:
        return ()
    if set(response) != {"matchedDeveloperMetadata"}:
        _reject("search response must contain only matchedDeveloperMetadata")
    matches = response["matchedDeveloperMetadata"]
    if type(matches) is not list:
        _reject("matchedDeveloperMetadata must be a list")
    if len(matches) > MAX_ROW_MARKER_MATCHES:
        _reject("row marker search exceeds the 128-match bound")

    parsed = []
    for match in matches:
        if type(match) is not dict or set(match) != {
            "developerMetadata",
            "dataFilters",
        }:
            _reject("each metadata match must contain exact wrapper fields")
        if (
            type(match["dataFilters"]) is not list
            or len(match["dataFilters"]) != 1
            or not _is_exact_marker_filter(
                match["dataFilters"][0],
                row_id=checked_row_id,
            )
        ):
            _reject("each metadata match must echo the exact lookup filter")
        observation = parse_row_developer_metadata(match["developerMetadata"])
        if observation["rowId"] != checked_row_id:
            _reject("returned metadata does not match the requested row ID")
        parsed.append(observation)

    parsed.sort(
        key=lambda item: (
            item["providerRowIndex"],
            item["metadataId"],
            item["rowId"],
        )
    )
    return tuple(parsed)
