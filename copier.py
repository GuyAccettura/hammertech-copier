"""
Core checklist-copy logic — no interactive prompts, no Flask dependencies.
Imported by app.py and usable standalone.
"""

from typing import Any, Dict, List, Set, Tuple
import requests

from auth import DEV_API_BASE, get_bearer_token

BASE_PATH = "company/api/ChecklistTypesApi"
DEFAULT_OBS_BASE_PATH = "company/api/ObservationTypes"

READONLY_KEYS = {
    "id", "companyid", "created", "createdby", "updated", "updatedby",
    "datecreated", "dateupdated", "lastmodified", "lastmodifieddate",
    "dateadded", "isdeleted", "version", "__metadata", "checklisttypeid",
    "checklistid", "issystemdefinedchecklisttype", "statustext",
}

TOP_LEVEL_DROP_KEYS = {"questions"}


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def make_base_url(instance: str, base_path: str = BASE_PATH) -> str:
    instance = instance.strip()
    if not instance:
        raise ValueError("Instance cannot be empty")
    if instance.endswith("hammertechonline.com"):
        instance = instance.split("//")[-1].split(".hammertechonline.com")[0]
    return f"https://{instance}.hammertechonline.com/{base_path.lstrip('/')}"


# ---------------------------------------------------------------------------
# Sanitize helpers
# ---------------------------------------------------------------------------

def sanitize(obj: Any, remove_keys: Set[str] = None) -> Any:
    if remove_keys is None:
        remove_keys = {k.lower() for k in READONLY_KEYS}
    if isinstance(obj, dict):
        return {
            k: sanitize(v, remove_keys)
            for k, v in obj.items()
            if k.lower() not in remove_keys
        }
    if isinstance(obj, list):
        return [sanitize(i, remove_keys) for i in obj]
    return obj


def sanitize_question(question: Dict[str, Any]) -> Dict[str, Any]:
    q = sanitize(question)
    for k in (
        "questionTypeImageUploadPhotoPreviewUrl",
        "relativeDataDwnldImageUrl",
        "relativeDataPopupImageUrl",
        "relativeImagePhotoPreviewUrl",
        "relativeImageFileName",
    ):
        q.pop(k, None)
    return q


# ---------------------------------------------------------------------------
# Response-extraction helpers
# ---------------------------------------------------------------------------

def extract_checklist_array(response_json: Any) -> List[Dict[str, Any]]:
    if isinstance(response_json, list):
        return response_json
    if isinstance(response_json, dict):
        for key in ("checkListTypes", "checklistTypes", "CheckListTypes"):
            if key in response_json and isinstance(response_json[key], list):
                return response_json[key]
        for val in response_json.values():
            if isinstance(val, list) and val and isinstance(val[0], dict):
                first = val[0]
                if any(k.lower() in ("id", "name", "type", "typedisplayname") for k in first):
                    return val
    return []


def extract_observation_type_array(response_json: Any) -> List[Dict[str, Any]]:
    if isinstance(response_json, list):
        return response_json
    if isinstance(response_json, dict):
        for key in (
            "observationTypes", "ObservationTypes", "observationTypeDtos",
            "items", "data", "results", "rows", "value",
        ):
            value = response_json.get(key)
            if isinstance(value, list):
                return value
        for value in response_json.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                keys = {str(k).lower() for k in value[0]}
                if "id" in keys and (
                    "name" in keys or "displayname" in keys or "observationtypename" in keys
                ):
                    return value
    return []


def normalize_name(name: str) -> str:
    return " ".join((name or "").strip().lower().split())


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def build_session(cookie: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Accept": "*/*",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Cookie": cookie,
        "User-Agent": "ht-checklist-copier/3.0",
    })
    return s


def get_json(session: requests.Session, url: str, timeout: int = 30) -> Any:
    r = session.get(url, timeout=timeout)
    r.raise_for_status()
    try:
        return r.json()
    except ValueError:
        return r.text


def get_json_detail(session: requests.Session, base_url: str, item_id: str) -> Any:
    return get_json(session, f"{base_url}/{item_id}")


def post_json(session: requests.Session, url: str, payload: Dict[str, Any]) -> requests.Response:
    return session.post(url, json=payload, timeout=60)


# ---------------------------------------------------------------------------
# Observation-type / Issue-type maps
# ---------------------------------------------------------------------------

def dedupe_obs_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: Set[str] = set()
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        obs_id = str(item.get("id") or item.get("Id") or "").strip()
        name = str(
            item.get("name") or item.get("Name")
            or item.get("displayName") or item.get("DisplayName")
            or item.get("observationTypeName") or ""
        ).strip()
        key = obs_id or name.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def build_issue_type_maps_via_dev_api(
    instance: str, email: str, password: str
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """
    Fetch IssueTypes from the developer API (bearer token auth).
    Returns (id_to_name, norm_name_to_id).
    """
    token = get_bearer_token(instance, email, password)
    r = requests.get(
        f"{DEV_API_BASE}/api/v1/IssueTypes",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    items = r.json()
    if not isinstance(items, list):
        raise ValueError(f"Unexpected IssueTypes response: {type(items)}")

    id_to_name: Dict[str, str] = {}
    norm_name_to_id: Dict[str, str] = {}
    for item in items:
        obs_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        if obs_id and name:
            id_to_name[obs_id] = name
            norm_name_to_id[normalize_name(name)] = obs_id
    return id_to_name, norm_name_to_id


def fetch_observation_types_via_session(
    session: requests.Session, base_url: str
) -> List[Dict[str, Any]]:
    """Fallback: paginate the company-API ObservationTypes endpoint."""
    page_size = 50
    aggregated: List[Dict[str, Any]] = []
    from_index = 0
    for _ in range(1000):
        payload = {
            "FromIndex": from_index,
            "Take": page_size,
            "ShouldSortAscending": True,
            "SortBy": "",
        }
        resp = session.post(base_url, json=payload, timeout=60)
        resp.raise_for_status()
        items = dedupe_obs_items(extract_observation_type_array(resp.json()))
        if not items:
            break
        aggregated.extend(items)
        if len(items) < page_size:
            break
        from_index += page_size
    aggregated = dedupe_obs_items(aggregated)
    if not aggregated:
        raise ValueError("No observation type rows found in paginated response.")
    return aggregated


def build_obs_maps_via_session(
    session: requests.Session, base_url: str
) -> Tuple[Dict[str, str], Dict[str, str]]:
    items = fetch_observation_types_via_session(session, base_url)
    id_to_name: Dict[str, str] = {}
    norm_name_to_id: Dict[str, str] = {}
    for item in items:
        obs_id = str(item.get("id") or item.get("Id") or "").strip()
        name = str(
            item.get("name") or item.get("Name")
            or item.get("displayName") or item.get("DisplayName")
            or item.get("observationTypeName") or ""
        ).strip()
        if obs_id and name:
            id_to_name[obs_id] = name
            norm_name_to_id[normalize_name(name)] = obs_id
    return id_to_name, norm_name_to_id


# ---------------------------------------------------------------------------
# ID remapping
# ---------------------------------------------------------------------------

def remap_default_issue_type_ids(
    obj: Any,
    src_id_to_name: Dict[str, str],
    dst_name_to_id: Dict[str, str],
    stats: Dict[str, int],
) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "defaultIssueTypeId":
                src_id = str(v or "").strip()
                if not src_id:
                    out[k] = ""
                    stats["blank"] += 1
                    continue
                src_name = src_id_to_name.get(src_id)
                if not src_name:
                    out[k] = ""
                    stats["source_id_not_found"] += 1
                    continue
                dst_id = dst_name_to_id.get(normalize_name(src_name))
                if not dst_id:
                    out[k] = ""
                    stats["destination_name_not_found"] += 1
                    continue
                out[k] = dst_id
                stats["mapped"] += 1
            else:
                out[k] = remap_default_issue_type_ids(v, src_id_to_name, dst_name_to_id, stats)
        return out
    if isinstance(obj, list):
        return [remap_default_issue_type_ids(i, src_id_to_name, dst_name_to_id, stats) for i in obj]
    return obj


# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------

def build_post_payload(
    detail_obj: Dict[str, Any],
    src_id_to_name: Dict[str, str],
    dst_name_to_id: Dict[str, str],
) -> Tuple[Dict[str, Any], Dict[str, int]]:
    src_questions = detail_obj.get("questions") or []
    cleaned = [sanitize_question(q) for q in src_questions if isinstance(q, dict)]
    cleaned.sort(key=lambda q: (q.get("zIndex") is None, q.get("zIndex", 0)))

    stats = {"mapped": 0, "blank": 0, "source_id_not_found": 0, "destination_name_not_found": 0}
    cleaned = remap_default_issue_type_ids(cleaned, src_id_to_name, dst_name_to_id, stats)

    top = sanitize(detail_obj)
    for key in list(top.keys()):
        if key in TOP_LEVEL_DROP_KEYS:
            top.pop(key, None)

    payload: Dict[str, Any] = {
        "name": top.get("name", ""),
        "displayName": top.get("displayName") or top.get("name", ""),
        "systemDefinedChecklistType": top.get("systemDefinedChecklistType", "-200"),
        "isHiddenFromMainList": bool(top.get("isHiddenFromMainList", False)),
        "checklistQuestions": cleaned,
    }
    for k in ("isHiddenFromMainList", "isInactive"):
        if k in top:
            payload[k] = top[k]

    return payload, stats


# ---------------------------------------------------------------------------
# High-level operations used by app.py
# ---------------------------------------------------------------------------

def fetch_checklists(session: requests.Session, instance: str) -> List[Dict[str, Any]]:
    """Return list of {id, name} dicts from the source instance."""
    base = make_base_url(instance, BASE_PATH)
    raw = get_json(session, base)
    if isinstance(raw, str):
        raise ValueError(f"Non-JSON response from {base}: {raw[:400]}")
    items = extract_checklist_array(raw) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise ValueError("Could not find checklist array in response.")
    result = []
    for item in items:
        name = (
            item.get("name") or item.get("Name")
            or item.get("typeDisplayName") or "<no name>"
        )
        idv = item.get("id") or item.get("Id") or item.get("ID") or "<no id>"
        result.append({"id": str(idv), "name": name})
    return result


def copy_checklists(
    src_instance: str,
    dst_instance: str,
    src_cookie: str,
    dst_cookie: str,
    checklist_ids: List[str],
    src_id_to_name: Dict[str, str],
    dst_name_to_id: Dict[str, str],
) -> List[Dict[str, Any]]:
    """
    Copy the given checklist IDs from src to dst.
    Returns a list of result dicts: {id, name, status, message}.
    """
    src_s = build_session(src_cookie)
    dst_s = build_session(dst_cookie)
    src_base = make_base_url(src_instance, BASE_PATH)
    dst_base = make_base_url(dst_instance, BASE_PATH)

    results = []
    for checklist_id in checklist_ids:
        entry: Dict[str, Any] = {"id": checklist_id, "name": checklist_id}
        try:
            detail = get_json_detail(src_s, src_base, checklist_id)
            if isinstance(detail, dict) and len(detail) == 1:
                lone_key = next(iter(detail))
                detail_obj = detail[lone_key] if isinstance(detail[lone_key], dict) else detail
            else:
                detail_obj = detail

            if not isinstance(detail_obj, dict):
                entry.update(status="error", message=f"Unexpected detail shape: {type(detail_obj)}")
                results.append(entry)
                continue

            entry["name"] = detail_obj.get("name") or checklist_id
            payload, stats = build_post_payload(detail_obj, src_id_to_name, dst_name_to_id)
            resp = post_json(dst_s, dst_base, payload)

            if 200 <= resp.status_code < 300:
                entry.update(
                    status="success",
                    message=f"HTTP {resp.status_code}",
                    questions=len(payload.get("checklistQuestions", [])),
                    mapping_stats=stats,
                )
            else:
                entry.update(
                    status="error",
                    message=f"HTTP {resp.status_code}: {(resp.text or '')[:400]}",
                )
        except Exception as exc:
            entry.update(status="error", message=str(exc))
        results.append(entry)
    return results
