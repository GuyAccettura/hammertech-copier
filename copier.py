"""
Core checklist-copy logic — no interactive prompts, no Flask dependencies.
Imported by app.py and usable standalone.
"""

from typing import Any, Dict, List, Set, Tuple
import re
import requests
from bs4 import BeautifulSoup

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
        "User-Agent": "ht-checklist-copier/3.0",
    })
    # Load cookies into the jar so the session can receive and send
    # updated cookies (e.g. __RequestVerificationToken) automatically.
    for pair in cookie.split(";"):
        pair = pair.strip()
        if "=" in pair:
            name, _, value = pair.partition("=")
            s.cookies.set(name.strip(), value.strip())
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

def fetch_obs_types_with_diff(
    src_session: requests.Session,
    dst_session: requests.Session,
    src_instance: str,
    dst_instance: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Fetch observation types from both instances.
    Returns (src_items, unique_to_src) where unique_to_src are source items
    whose normalised name does not exist in the destination.
    """
    src_base = make_base_url(src_instance, DEFAULT_OBS_BASE_PATH)
    dst_base = make_base_url(dst_instance, DEFAULT_OBS_BASE_PATH)

    src_items = fetch_observation_types_via_session(src_session, src_base)
    dst_items = fetch_observation_types_via_session(dst_session, dst_base)

    dst_names = {
        normalize_name(item.get("name") or item.get("Name") or "")
        for item in dst_items
    }

    unique = [
        item for item in src_items
        if normalize_name(item.get("name") or item.get("Name") or "") not in dst_names
    ]
    return src_items, unique


def fetch_issue_categories(instance: str, email: str, password: str) -> List[Dict[str, Any]]:
    """Fetch issue categories from the dev API. Returns [{id, name}, ...]."""
    from auth import DEV_API_BASE, get_bearer_token
    token = get_bearer_token(instance, email, password)
    r = requests.get(
        f"{DEV_API_BASE}/api/v1/IssueCategories",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    items = r.json()
    if not isinstance(items, list):
        raise ValueError(f"Unexpected IssueCategories response: {type(items)}")
    return items


def build_category_maps(
    src_instance: str,
    dst_instance: str,
    email: str,
    password: str,
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """
    Returns (src_id_to_name, dst_name_to_id) for issue categories,
    used to remap CategoryId values when copying observation types.
    """
    src_cats = fetch_issue_categories(src_instance, email, password)
    dst_cats = fetch_issue_categories(dst_instance, email, password)

    src_id_to_name: Dict[str, str] = {
        str(c.get("id", "")): str(c.get("name", ""))
        for c in src_cats if c.get("id") and c.get("name")
    }
    dst_name_to_id: Dict[str, str] = {
        normalize_name(str(c.get("name", ""))): str(c.get("id", ""))
        for c in dst_cats if c.get("id") and c.get("name")
    }
    return src_id_to_name, dst_name_to_id


# Integer → string enum mappings between GET and POST shapes
_CAN_RAISE_IN_MAP: Dict[int, str] = {
    0: "ObservationsModule",
    1: "Meetings",
    2: "CustomSections",
    3: "PreTaskPlans",
    4: "SiteDiary",
    5: "Incidents",
}

_PRIORITY_MAP: Dict[int, str] = {
    0: "low",
    1: "medium",
    2: "high",
    3: "critical",
}

_CUSTOM_FIELD_TYPE_MAP: Dict[int, str] = {
    0:  "FreeText",
    1:  "TextArea",
    2:  "Checkbox",
    3:  "Dropdown",
    5:  "Separator",
    6:  "Heading",
    7:  "ImageUpload",
    8:  "Date",
    9:  "Time",
    10: "DateTime",
    11: "YesNoRadio",
    12: "BigLabel",
    13: "NoMargin",
    14: "ExpiryDate",
    15: "ExpandingLabel",
    16: "Signature",
    17: "SignatureWithName",
    19: "YesNoNaRadio",
    20: "FileUpload",
    21: "Image",
    22: "FileDownload",
    23: "MultiSelectDropdown",
    24: "SectionStart",
    25: "SectionEnd",
    27: "Number",
}

# Field types that reference source-environment files — strip EntityId for these
_FILE_FIELD_TYPES = {21, 22}  # Image (download), FileDownload

# Fields to strip from custom field objects before POSTing
_CUSTOM_FIELD_STRIP = {
    "id", "entityid", "uploadresult", "answeroptions",
    "istabularsubform", "canuseredit",
}


def _sanitize_custom_field(field: Dict[str, Any]) -> Dict[str, Any]:
    """Strip readonly/internal fields and remap CustomFieldType int → string."""
    out = {
        k: v for k, v in field.items()
        if k.lower() not in _CUSTOM_FIELD_STRIP
    }
    cft_raw = field.get("CustomFieldType")
    if isinstance(cft_raw, int):
        mapped = _CUSTOM_FIELD_TYPE_MAP.get(cft_raw)
        if mapped:
            out["CustomFieldType"] = mapped
        else:
            print(f"  [CFT] Unknown CustomFieldType integer: {cft_raw} — please add to _CUSTOM_FIELD_TYPE_MAP")
            out["CustomFieldType"] = str(cft_raw)
        # Strip EntityId for file types — it references source-environment files
        if cft_raw in _FILE_FIELD_TYPES:
            out.pop("EntityId", None)
    # Ensure LocalisedAnswerOptions is present
    if "LocalisedAnswerOptions" not in out:
        out["LocalisedAnswerOptions"] = []
    return out


def _build_obs_type_create_payload(
    item: Dict[str, Any],
    src_cat_id_to_name: Dict[str, str],
    dst_cat_name_to_id: Dict[str, str],
) -> Dict[str, Any]:
    """
    Map a source obs type (GET shape) to the Create endpoint payload shape.

    GET uses: Category (obj), CanBeNegative/Positive/Neutral (bools), SuggestedFunctions
    POST needs: CategoryId (str), AllowableClassifications (list), CanRaiseIn (list)
    """
    def _get(key: str, default=None):
        # Try PascalCase (source) then camelCase
        camel = key[0].lower() + key[1:]
        return item.get(key, item.get(camel, default))

    # --- AllowableClassifications from boolean flags ---
    classifications = []
    if _get("CanBeNegative"):
        classifications.append("Negative")
    if _get("CanBePositive"):
        classifications.append("Positive")
    if _get("CanBeNeutral"):
        classifications.append("Neutral")

    # --- CategoryId: resolve from Category object by name ---
    import re as _re
    _UUID_RE = _re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', _re.I)

    category = _get("Category") or {}
    if isinstance(category, dict):
        src_cat_id = str(category.get("Id") or category.get("id") or "").strip()
        src_cat_name = str(category.get("Name") or category.get("name") or "").strip()
    else:
        cat_str = str(category).strip()
        if _UUID_RE.match(cat_str):
            # It's an ID — resolve to name via the map
            src_cat_id = cat_str
            src_cat_name = src_cat_id_to_name.get(src_cat_id, "")
        else:
            # It's already a name
            src_cat_name = cat_str
            src_cat_id = ""

    if src_cat_name:
        dst_cat_id = dst_cat_name_to_id.get(normalize_name(src_cat_name), "")
        print(f"  [CAT] src='{src_cat_name}' → normalized='{normalize_name(src_cat_name)}' → dst_id='{dst_cat_id}'")
        print(f"  [CAT] available dst categories: {list(dst_cat_name_to_id.keys())}")
        if not dst_cat_id:
            print(f"  ! No matching category in destination for '{src_cat_name}' — leaving CategoryId blank.")
    elif src_cat_id:
        src_cat_name = src_cat_id_to_name.get(src_cat_id, "")
        dst_cat_id = dst_cat_name_to_id.get(normalize_name(src_cat_name), "") if src_cat_name else ""
        print(f"  [CAT] src_id='{src_cat_id}' → src_name='{src_cat_name}' → dst_id='{dst_cat_id}'")
    else:
        dst_cat_id = ""
        print(f"  [CAT] no category on source item")

    # --- CanRaiseIn: map integers → strings ---
    raw_raise_in = _get("CanRaiseIn") or []
    can_raise_in = [
        _CAN_RAISE_IN_MAP.get(v, str(v)) if isinstance(v, int) else v
        for v in raw_raise_in
    ] or ["ObservationsModule"]

    # --- SuggestedPriority: map integer → string ---
    raw_priority = _get("SuggestedPriority")
    if isinstance(raw_priority, int):
        priority = _PRIORITY_MAP.get(raw_priority, "medium")
    else:
        priority = raw_priority or "medium"

    # --- CustomFields: strip internals, remap type ---
    def _clean_fields(fields):
        if not isinstance(fields, list):
            return []
        return [_sanitize_custom_field(f) for f in fields if isinstance(f, dict)]

    # --- WhoCanCreate ---
    who_can_create = []
    if _get("CanBeCreatedByWorker"):
        who_can_create.append("Workers")
    if _get("CanBeCreatedByEmployer"):
        who_can_create.append("Employers")

    return {
        "Name": _get("Name", ""),
        "NameLocalisations": _get("NameLocalisations", []),
        "CategoryId": dst_cat_id,
        "Colour": _get("Colour", "#808080"),
        "AllowableClassifications": classifications,
        "SuggestedPriority": priority,
        "ForcePriority": _get("ForcePriority", False),
        "SuggestedFunctions": _get("SuggestedFunctions", []),
        "CanRaiseIn": can_raise_in,
        "WhoCanCreate": who_can_create,
        "CustomFieldsForOpening": _clean_fields(_get("CustomFieldsForOpening")),
        "CustomFieldsForClosing": _clean_fields(_get("CustomFieldsForClosing")),
    }


def copy_observation_types(
    dst_session: requests.Session,
    dst_instance: str,
    selected_items: List[Dict[str, Any]],
    src_cat_id_to_name: Dict[str, str],
    dst_cat_name_to_id: Dict[str, str],
    src_session: requests.Session = None,
    src_instance: str = None,
) -> List[Dict[str, Any]]:
    """
    POST each selected observation type to the destination instance.
    Fetches full detail for each item from source to get CanRaiseIn and CustomFields.
    Returns a list of result dicts: {name, status, message}.
    """
    dst_base = make_base_url(dst_instance, DEFAULT_OBS_BASE_PATH + "/Create")
    src_detail_base = make_base_url(src_instance, DEFAULT_OBS_BASE_PATH) if src_instance else None
    results = []
    for item in selected_items:
        name = item.get("name") or item.get("Name") or str(item.get("id", "unknown"))
        entry: Dict[str, Any] = {"name": name}
        try:
            # Fetch full detail from source to get CanRaiseIn and CustomFields
            if src_session and src_detail_base:
                item_id = item.get("Id") or item.get("id") or ""
                if item_id:
                    detail = get_json(src_session, f"{src_detail_base}/{item_id}")
                    if isinstance(detail, dict):
                        item = detail
            payload = _build_obs_type_create_payload(item, src_cat_id_to_name, dst_cat_name_to_id)
            resp = dst_session.post(dst_base, json=payload, timeout=60)
            print(f"[DEBUG] OBS TYPE '{name}' → {resp.status_code}: {resp.text[:300]}")
            if 200 <= resp.status_code < 300:
                entry.update(status="success", message=f"HTTP {resp.status_code}: {resp.text[:200]}")
            else:
                entry.update(
                    status="error",
                    message=f"HTTP {resp.status_code}: {(resp.text or '')[:400]}",
                )
        except Exception as exc:
            entry.update(status="error", message=str(exc))
        results.append(entry)
    return results


# ---------------------------------------------------------------------------
# Job Titles (MVC HTML scrape + form POST)
# ---------------------------------------------------------------------------

JOB_TITLES_LIST_PATH = "company/Internal/JobTitles"
JOB_TITLES_CREATE_PATH = "company/Internal/JobTitles/Create"


def fetch_job_titles(session: requests.Session, instance: str) -> List[Dict[str, str]]:
    """Scrape the Job Titles list page and return [{id, name}]."""
    url = f"https://{instance}.hammertechonline.com/{JOB_TITLES_LIST_PATH}"
    r = session.get(url, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    results = []
    for a in soup.select("a.table-row-button"):
        href = a.get("href", "")
        name = a.get_text(strip=True)
        m = re.search(r"/Details/([^/]+)$", href)
        if m and name:
            results.append({"id": m.group(1), "name": name})
    return results


def fetch_job_titles_with_diff(
    src_session: requests.Session,
    dst_session: requests.Session,
    src_instance: str,
    dst_instance: str,
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """Returns (src_items, unique_to_src) — items whose name doesn't exist in dst."""
    src_items = fetch_job_titles(src_session, src_instance)
    dst_items = fetch_job_titles(dst_session, dst_instance)
    dst_names = {normalize_name(i["name"]) for i in dst_items}
    unique = [i for i in src_items if normalize_name(i["name"]) not in dst_names]
    return src_items, unique


def copy_job_titles(
    dst_session: requests.Session,
    dst_instance: str,
    selected_names: List[str],
) -> List[Dict[str, Any]]:
    """POST each job title name to the destination using the MVC form endpoint."""
    create_url = f"https://{dst_instance}.hammertechonline.com/{JOB_TITLES_CREATE_PATH}"
    # MVC form POST requires form encoding, not JSON
    form_headers = {"Content-Type": "application/x-www-form-urlencoded"}
    results = []
    for name in selected_names:
        entry: Dict[str, Any] = {"name": name}
        try:
            csrf = _get_csrf_token_for_path(dst_session, dst_instance, JOB_TITLES_CREATE_PATH)
            data = {"__RequestVerificationToken": csrf, "Name": name}
            resp = dst_session.post(create_url, data=data, headers=form_headers,
                                    timeout=30, allow_redirects=False)
            if resp.status_code in (200, 201, 302):
                entry.update(status="success", message=f"HTTP {resp.status_code}")
            else:
                entry.update(
                    status="error",
                    message=f"HTTP {resp.status_code}: {(resp.text or '')[:400]}",
                )
        except Exception as exc:
            entry.update(status="error", message=str(exc))
        results.append(entry)
    return results


# ---------------------------------------------------------------------------
# Licenses (MVC HTML scrape + form POST)
# ---------------------------------------------------------------------------

LICENSES_LIST_PATH = "company/Internal/Licenses"
LICENSES_CREATE_PATH = "company/Internal/Licenses/Create"
LICENSES_EDIT_PATH = "company/Internal/Licenses/Edit"

# MVC checkbox fields — sent as FieldName=true&FieldName=false when checked,
# or FieldName=false when unchecked
_LICENSE_CHECKBOX_FIELDS = [
    "IsPriority",
    "IsCompulsoryForInduction",
    "HasExpiryDate",
    "HasIssueDate",
    "HasRefreshmentDate",
    "HasIssuer",
    "HasLicenseNo",
    "HasLicensePhoto",
    "IsLicenceFrontPhotoMandatory",
    "IsLicenceBackPhotoMandatory",
    "IsFileUploadEnabled",
    "IsFileUploadRequired",
]


def fetch_licenses(session: requests.Session, instance: str) -> List[Dict[str, str]]:
    """Scrape the Licenses list page and return [{id, name}]."""
    url = f"https://{instance}.hammertechonline.com/{LICENSES_LIST_PATH}"
    r = session.get(url, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    # Each row has multiple links all pointing to the same license ID.
    # Column order: [0]=Category, [1]=Name, [2]=Code, [3+]=Y/N flags.
    # Group links by ID and take index [1] (the name column).
    from collections import defaultdict
    links_by_id: dict = defaultdict(list)
    order: list = []
    for a in soup.select("a.table-row-button"):
        href = a.get("href", "")
        m = re.search(r"/([0-9a-fA-F-]{36})$", href)
        if not m:
            continue
        lid = m.group(1)
        if lid not in links_by_id:
            order.append(lid)
        links_by_id[lid].append(a.get_text(strip=True))

    results = []
    for lid in order:
        texts = links_by_id[lid]
        if len(texts) >= 2 and texts[1]:
            results.append({"id": lid, "name": texts[1]})
    return results


def fetch_license_detail(session: requests.Session, instance: str, license_id: str) -> Dict[str, Any]:
    """Scrape the Edit page for a license and return its field values."""
    url = f"https://{instance}.hammertechonline.com/{LICENSES_EDIT_PATH}/{license_id}"
    r = session.get(url, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    def val(name):
        el = soup.find("input", {"name": name})
        return el["value"] if el and el.get("value") else ""

    def is_checked(name):
        el = soup.find("input", {"name": name, "type": "checkbox"})
        return el is not None and el.get("checked") is not None

    # Category select
    cat_select = soup.find("select", {"name": "Category"})
    category = "0"
    if cat_select:
        selected = cat_select.find("option", selected=True)
        if selected:
            category = selected.get("value", "0")

    return {
        "Name": val("Name"),
        "Code": val("Code"),
        "Category": category,
        "IsPriority": is_checked("IsPriority"),
        "IsCompulsoryForInduction": is_checked("IsCompulsoryForInduction"),
        "HasExpiryDate": is_checked("HasExpiryDate"),
        "HasIssueDate": is_checked("HasIssueDate"),
        "HasRefreshmentDate": is_checked("HasRefreshmentDate"),
        "HasIssuer": is_checked("HasIssuer"),
        "HasLicenseNo": is_checked("HasLicenseNo"),
        "HasLicensePhoto": is_checked("HasLicensePhoto"),
        "IsLicenceFrontPhotoMandatory": is_checked("IsLicenceFrontPhotoMandatory"),
        "IsLicenceBackPhotoMandatory": is_checked("IsLicenceBackPhotoMandatory"),
        "IsFileUploadEnabled": is_checked("IsFileUploadEnabled"),
        "IsFileUploadRequired": is_checked("IsFileUploadRequired"),
    }


def fetch_licenses_with_diff(
    src_session: requests.Session,
    dst_session: requests.Session,
    src_instance: str,
    dst_instance: str,
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """Returns (src_items, unique_to_src) — items whose name doesn't exist in dst."""
    src_items = fetch_licenses(src_session, src_instance)
    dst_items = fetch_licenses(dst_session, dst_instance)
    dst_names = {normalize_name(i["name"]) for i in dst_items}
    unique = [i for i in src_items if normalize_name(i["name"]) not in dst_names]
    return src_items, unique


def _get_csrf_token_for_path(session: requests.Session, instance: str, path: str) -> str:
    """Fetch any MVC page and extract the ASP.NET anti-forgery token."""
    url = f"https://{instance}.hammertechonline.com/{path}"
    r = session.get(url, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    token_input = soup.find("input", {"name": "__RequestVerificationToken"})
    if not token_input:
        raise ValueError(f"Could not find __RequestVerificationToken on {path}")
    return token_input["value"]


def copy_licenses(
    src_session: requests.Session,
    src_instance: str,
    dst_session: requests.Session,
    dst_instance: str,
    selected_items: List[Dict[str, str]],
) -> List[Dict[str, Any]]:
    """Scrape each license's Edit page then POST to destination Create."""
    create_url = f"https://{dst_instance}.hammertechonline.com/{LICENSES_CREATE_PATH}"
    form_headers = {"Content-Type": "application/x-www-form-urlencoded"}
    results = []
    for item in selected_items:
        name = item["name"]
        entry: Dict[str, Any] = {"name": name}
        try:
            detail = fetch_license_detail(src_session, src_instance, item["id"])
            csrf = _get_csrf_token_for_path(dst_session, dst_instance, LICENSES_CREATE_PATH)

            # Build list-of-tuples to handle MVC duplicate checkbox pattern
            data: List[Tuple[str, str]] = [
                ("__RequestVerificationToken", csrf),
                ("Category", detail["Category"]),
                ("Name", detail["Name"]),
                ("Code", detail["Code"]),
            ]
            for field in _LICENSE_CHECKBOX_FIELDS:
                if detail.get(field):
                    data.append((field, "true"))
                data.append((field, "false"))

            resp = dst_session.post(create_url, data=data, headers=form_headers,
                                    timeout=30, allow_redirects=False)
            if resp.status_code in (200, 201, 302):
                entry.update(status="success", message=f"HTTP {resp.status_code}")
            else:
                entry.update(
                    status="error",
                    message=f"HTTP {resp.status_code}: {(resp.text or '')[:400]}",
                )
        except Exception as exc:
            entry.update(status="error", message=str(exc))
        results.append(entry)
    return results


# ---------------------------------------------------------------------------
# Meeting Types path
# ---------------------------------------------------------------------------

MEETING_TYPES_LIST_PATH = "company/Internal/MeetingTypes"
MEETING_TYPES_CREATE_PATH = "company/Internal/MeetingTypes/Create"
MEETING_TYPES_EDIT_PATH = "company/Internal/MeetingTypes/Edit"

_MT_BOOL_FIELDS = [
    "IsEmployerAllowedToCreate",
    "IsAttendeeLocationsEnabled",
    "IsAvailableToSignInDevices",
    "IsMandatoryInSignInDevices",
    "IsAttendeesMustDownloadPDFToSign",
    "IsAllowedToClone",
    "EnableSiteSignIn",
]


def fetch_meeting_types(session: requests.Session, instance: str) -> List[Dict[str, str]]:
    """Scrape the MeetingTypes list page and return [{id, name}]."""
    url = f"https://{instance}.hammertechonline.com/{MEETING_TYPES_LIST_PATH}"
    r = session.get(url, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    results = []
    for row in soup.select("tr.table-row-button"):
        onclick = row.get("onclick", "")
        m = re.search(r"/([0-9a-fA-F-]{36})'", onclick)
        if not m:
            continue
        lid = m.group(1)
        first_td = row.find("td")
        name = first_td.get_text(strip=True) if first_td else ""
        if name:
            results.append({"id": lid, "name": name})
    return results


def fetch_meeting_type_detail(
    session: requests.Session, instance: str, mt_id: str
) -> Dict[str, Any]:
    """Scrape the Edit page for a meeting type and return its field values."""
    url = f"https://{instance}.hammertechonline.com/{MEETING_TYPES_EDIT_PATH}/{mt_id}"
    r = session.get(url, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    def val(name: str) -> str:
        el = soup.find("input", {"name": name})
        return el["value"] if el and el.get("value") else ""

    def is_checked(name: str) -> bool:
        el = soup.find("input", {"name": name, "type": "checkbox"})
        return el is not None and el.get("checked") is not None

    _EMPTY_GUID = "00000000-0000-0000-0000-000000000000"
    extra_fields: list = []

    for inp in soup.find_all("input"):
        name = inp.get("name", "")
        if not name or "{{" in name:
            continue
        if name == "__RequestVerificationToken":
            continue
        if not (name.startswith("_SystemFields") or name.startswith("_CustomFieldForm")):
            continue
        inp_type = (inp.get("type") or "text").lower()
        if inp_type == "file":
            continue
        if inp_type == "checkbox":
            if inp.get("checked") is not None:
                extra_fields.append((name, inp.get("value") or "true"))
        else:
            value = inp.get("value") or ""
            if re.search(r"_CustomFieldForm\[\d+\]\.Id$", name):
                value = _EMPTY_GUID
            extra_fields.append((name, value))

    for sel in soup.find_all("select"):
        name = sel.get("name", "")
        if not name or "{{" in name:
            continue
        if not (name.startswith("_SystemFields") or name.startswith("_CustomFieldForm")):
            continue
        selected_opt = sel.find("option", selected=True)
        value = selected_opt["value"] if selected_opt and selected_opt.get("value") else ""
        extra_fields.append((name, value))

    return {
        "Name": val("Name"),
        **{field: is_checked(field) for field in _MT_BOOL_FIELDS},
        "_extra_fields": extra_fields,
    }


def fetch_meeting_types_with_diff(
    src_session: requests.Session,
    dst_session: requests.Session,
    src_instance: str,
    dst_instance: str,
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """Returns (src_items, unique_to_src) — items whose name doesn't exist in dst."""
    src_items = fetch_meeting_types(src_session, src_instance)
    dst_items = fetch_meeting_types(dst_session, dst_instance)
    dst_names = {normalize_name(i["name"]) for i in dst_items}
    unique = [i for i in src_items if normalize_name(i["name"]) not in dst_names]
    return src_items, unique


def _scrape_dst_project_fields(session: requests.Session, instance: str) -> List[tuple]:
    """Scrape all applicableProjectIds and applicableProjectRegions from the Create page."""
    url = f"https://{instance}.hammertechonline.com/{MEETING_TYPES_CREATE_PATH}"
    r = session.get(url, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    fields = []
    for inp in soup.find_all("input"):
        name = inp.get("name", "")
        if name not in ("applicableProjectIds", "applicableProjectRegions"):
            continue
        value = inp.get("value", "")
        if value:
            fields.append((name, value))
    return fields


def _filter_subform_fields(extra_fields: List[tuple]) -> tuple:
    """
    Remove any _CustomFieldForm[N] groups where FieldType == 'SubForm'.
    Returns (filtered_fields, list_of_skipped_field_names).
    """
    # Find indices of SubForm fields
    subform_indices = set()
    for name, value in extra_fields:
        m = re.match(r"_CustomFieldForm\[(\d+)\]\.FieldType$", name)
        if m and str(value).lower() == "subform":
            subform_indices.add(m.group(1))

    if not subform_indices:
        return extra_fields, []

    # Collect names of skipped fields for reporting
    skipped_names = []
    for name, value in extra_fields:
        m = re.match(r"_CustomFieldForm\[(\d+)\]\.Name$", name)
        if m and m.group(1) in subform_indices:
            skipped_names.append(value)

    # Strip all tuples belonging to those indices
    filtered = [
        (name, value) for name, value in extra_fields
        if not any(
            re.match(rf"_CustomFieldForm\[{idx}\]\.", name)
            for idx in subform_indices
        )
    ]
    return filtered, skipped_names


def copy_meeting_types(
    src_session: requests.Session,
    src_instance: str,
    dst_session: requests.Session,
    dst_instance: str,
    selected_items: List[Dict[str, str]],
) -> List[Dict[str, Any]]:
    """Scrape each meeting type's Edit page then POST to destination Create."""
    create_url = f"https://{dst_instance}.hammertechonline.com/{MEETING_TYPES_CREATE_PATH}"
    form_headers = {"Content-Type": "application/x-www-form-urlencoded"}

    # Scrape destination projects once — select all by default
    try:
        dst_project_fields = _scrape_dst_project_fields(dst_session, dst_instance)
    except Exception:
        dst_project_fields = []

    results = []
    for item in selected_items:
        name = item["name"]
        entry: Dict[str, Any] = {"name": name}
        try:
            detail = fetch_meeting_type_detail(src_session, src_instance, item["id"])
            csrf = _get_csrf_token_for_path(dst_session, dst_instance, MEETING_TYPES_CREATE_PATH)

            extra_fields, skipped = _filter_subform_fields(detail["_extra_fields"])

            data: list = [
                ("__RequestVerificationToken", csrf),
                ("Name", detail["Name"]),
            ]
            for field in _MT_BOOL_FIELDS:
                if detail.get(field):
                    data.append((field, "true"))
                data.append((field, "false"))
            data.extend(dst_project_fields)
            data.append(("addToFutureProjectsByRegion", "All"))
            data.extend(extra_fields)

            resp = dst_session.post(create_url, data=data, headers=form_headers,
                                    timeout=30, allow_redirects=False)
            if resp.status_code in (200, 201, 302):
                msg = f"HTTP {resp.status_code}"
                if skipped:
                    msg += f" — SubForm fields skipped (manual setup required): {', '.join(skipped)}"
                entry.update(status="success", message=msg)
            else:
                entry.update(
                    status="error",
                    message=f"HTTP {resp.status_code}: {(resp.text or '')[:400]}",
                )
        except Exception as exc:
            entry.update(status="error", message=str(exc))
        results.append(entry)
    return results


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
