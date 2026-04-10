"""
Flask web app for HammerTech checklist + observation-type copier.

Flow:
  1. GET  /                      → login form
  2. POST /auth                  → Playwright login → /home/<job>
  3. GET  /home/<job>            → tool selector (checklists | obs types)

  Checklist path:
  4. GET  /select/<job>          → fetch + show checklist selection
  5. POST /copy/<job>            → run checklist copy → results

  Observation Type path:
  4. GET  /obs-types/<job>       → fetch + diff obs types → show selection
  5. POST /copy-obs-types/<job>  → run obs type copy → results
"""

import os
import uuid
from typing import Any, Dict

from flask import Flask, render_template, request, redirect, url_for

import auth as ht_auth
import copier as ht_copy

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")

# In-memory job store — fine for single-worker deployment.
_jobs: Dict[str, Dict[str, Any]] = {}


def _expired():
    return render_template("index.html", step="login", error="Session expired. Please log in again.")


# ---------------------------------------------------------------------------
# Login form
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return render_template("index.html", step="login", error=None)


# ---------------------------------------------------------------------------
# Authenticate both instances → tool selector
# ---------------------------------------------------------------------------

@app.post("/auth")
def authenticate():
    src_instance = (request.form.get("src_instance") or "").strip()
    dst_instance = (request.form.get("dst_instance") or "").strip()
    email = (request.form.get("email") or "").strip()
    password = (request.form.get("password") or "").strip()

    if not all([src_instance, dst_instance, email, password]):
        return render_template("index.html", step="login", error="All fields are required.")

    try:
        src_cookie = ht_auth.get_auth_cookie_playwright(src_instance, email, password)
        print(f"[DEBUG] SRC cookie names: {[p.split('=')[0] for p in src_cookie.split('; ')]}")
    except Exception as exc:
        return render_template("index.html", step="login",
                               error=f"Login failed for SOURCE '{src_instance}': {exc}")

    try:
        dst_cookie = ht_auth.get_auth_cookie_playwright(dst_instance, email, password)
        print(f"[DEBUG] DST cookie names: {[p.split('=')[0] for p in dst_cookie.split('; ')]}")
    except Exception as exc:
        return render_template("index.html", step="login",
                               error=f"Login failed for DESTINATION '{dst_instance}': {exc}")

    job_id = str(uuid.uuid4())
    _jobs[job_id] = {
        "src_instance": src_instance,
        "dst_instance": dst_instance,
        "src_cookie": src_cookie,
        "dst_cookie": dst_cookie,
        "email": email,
        "password": password,
    }
    return redirect(url_for("home", job_id=job_id))


# ---------------------------------------------------------------------------
# Tool selector
# ---------------------------------------------------------------------------

@app.get("/home/<job_id>")
def home(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return _expired()
    return render_template("index.html", step="home", job_id=job_id,
                           src_instance=job["src_instance"],
                           dst_instance=job["dst_instance"])


# ---------------------------------------------------------------------------
# Checklist path — select
# ---------------------------------------------------------------------------

@app.get("/select/<job_id>")
def select_checklists(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return _expired()

    # Fetch checklists + issue-type maps lazily on first visit
    if "checklists" not in job:
        src_s = ht_copy.build_session(job["src_cookie"])
        try:
            job["checklists"] = ht_copy.fetch_checklists(src_s, job["src_instance"])
        except Exception as exc:
            return render_template("index.html", step="home", job_id=job_id,
                                   src_instance=job["src_instance"],
                                   dst_instance=job["dst_instance"],
                                   error=f"Could not load checklists: {exc}")

        try:
            job["src_id_to_name"], _ = ht_copy.build_issue_type_maps_via_dev_api(
                job["src_instance"], job["email"], job["password"])
            _, job["dst_name_to_id"] = ht_copy.build_issue_type_maps_via_dev_api(
                job["dst_instance"], job["email"], job["password"])
            job["issue_type_warning"] = None
        except Exception as exc:
            job["src_id_to_name"] = {}
            job["dst_name_to_id"] = {}
            job["issue_type_warning"] = (
                f"Could not load Issue Types (defaultIssueTypeId will be cleared): {exc}"
            )

    return render_template("index.html", step="select", job_id=job_id,
                           src_instance=job["src_instance"],
                           dst_instance=job["dst_instance"],
                           checklists=job["checklists"],
                           issue_type_warning=job.get("issue_type_warning"))


# ---------------------------------------------------------------------------
# Checklist path — copy
# ---------------------------------------------------------------------------

@app.post("/copy/<job_id>")
def run_copy(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return _expired()

    selected_ids = request.form.getlist("checklist_ids")
    if not selected_ids:
        return render_template("index.html", step="select", job_id=job_id,
                               src_instance=job["src_instance"],
                               dst_instance=job["dst_instance"],
                               checklists=job.get("checklists", []),
                               issue_type_warning=job.get("issue_type_warning"),
                               error="Select at least one checklist.")

    results = ht_copy.copy_checklists(
        src_instance=job["src_instance"],
        dst_instance=job["dst_instance"],
        src_cookie=job["src_cookie"],
        dst_cookie=job["dst_cookie"],
        checklist_ids=selected_ids,
        src_id_to_name=job.get("src_id_to_name", {}),
        dst_name_to_id=job.get("dst_name_to_id", {}),
    )

    return render_template("index.html", step="results",
                           job_id=job_id,
                           src_instance=job["src_instance"],
                           dst_instance=job["dst_instance"],
                           results=results,
                           result_type="checklists")


# ---------------------------------------------------------------------------
# Observation Type path — select (fetch + diff)
# ---------------------------------------------------------------------------

@app.get("/obs-types/<job_id>")
def select_obs_types(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return _expired()

    # Fetch + diff lazily on first visit
    if "obs_unique" not in job:
        src_s = ht_copy.build_session(job["src_cookie"])
        dst_s = ht_copy.build_session(job["dst_cookie"])
        try:
            src_all, unique = ht_copy.fetch_obs_types_with_diff(
                src_s, dst_s, job["src_instance"], job["dst_instance"]
            )
            job["obs_src_all"] = src_all
            job["obs_unique"] = unique
        except Exception as exc:
            return render_template("index.html", step="home", job_id=job_id,
                                   src_instance=job["src_instance"],
                                   dst_instance=job["dst_instance"],
                                   error=f"Could not load Observation Types: {exc}")

        try:
            job["src_cat_id_to_name"], job["dst_cat_name_to_id"] = ht_copy.build_category_maps(
                job["src_instance"], job["dst_instance"],
                job["email"], job["password"],
            )
        except Exception as exc:
            print(f"[WARN] Could not load issue categories: {exc}")
            job["src_cat_id_to_name"] = {}
            job["dst_cat_name_to_id"] = {}

    return render_template("index.html", step="select_obs", job_id=job_id,
                           src_instance=job["src_instance"],
                           dst_instance=job["dst_instance"],
                           obs_unique=job["obs_unique"])


# ---------------------------------------------------------------------------
# Observation Type path — copy
# ---------------------------------------------------------------------------

@app.post("/copy-obs-types/<job_id>")
def run_copy_obs_types(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return _expired()

    selected_names = set(request.form.getlist("obs_type_names"))
    if not selected_names:
        return render_template("index.html", step="select_obs", job_id=job_id,
                               src_instance=job["src_instance"],
                               dst_instance=job["dst_instance"],
                               obs_unique=job.get("obs_unique", []),
                               error="Select at least one observation type.")

    selected_items = [
        item for item in job.get("obs_unique", [])
        if (item.get("name") or item.get("Name") or "") in selected_names
    ]

    src_s = ht_copy.build_session(job["src_cookie"])
    dst_s = ht_copy.build_session(job["dst_cookie"])
    results = ht_copy.copy_observation_types(
        dst_s, job["dst_instance"], selected_items,
        src_cat_id_to_name=job.get("src_cat_id_to_name", {}),
        dst_cat_name_to_id=job.get("dst_cat_name_to_id", {}),
        src_session=src_s,
        src_instance=job["src_instance"],
    )

    return render_template("index.html", step="results",
                           job_id=job_id,
                           src_instance=job["src_instance"],
                           dst_instance=job["dst_instance"],
                           results=results,
                           result_type="obs_types")


# ---------------------------------------------------------------------------
# Job Titles path — select (fetch + diff)
# ---------------------------------------------------------------------------

@app.get("/job-titles/<job_id>")
def select_job_titles(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return _expired()

    if "job_titles_unique" not in job:
        src_s = ht_copy.build_session(job["src_cookie"])
        dst_s = ht_copy.build_session(job["dst_cookie"])
        try:
            _, unique = ht_copy.fetch_job_titles_with_diff(
                src_s, dst_s, job["src_instance"], job["dst_instance"]
            )
            job["job_titles_unique"] = unique
        except Exception as exc:
            return render_template("index.html", step="home", job_id=job_id,
                                   src_instance=job["src_instance"],
                                   dst_instance=job["dst_instance"],
                                   error=f"Could not load Job Titles: {exc}")

    return render_template("index.html", step="select_job_titles", job_id=job_id,
                           src_instance=job["src_instance"],
                           dst_instance=job["dst_instance"],
                           job_titles_unique=job["job_titles_unique"])


# ---------------------------------------------------------------------------
# Job Titles path — copy
# ---------------------------------------------------------------------------

@app.post("/copy-job-titles/<job_id>")
def run_copy_job_titles(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return _expired()

    selected_names = request.form.getlist("job_title_names")
    if not selected_names:
        return render_template("index.html", step="select_job_titles", job_id=job_id,
                               src_instance=job["src_instance"],
                               dst_instance=job["dst_instance"],
                               job_titles_unique=job.get("job_titles_unique", []),
                               error="Select at least one job title.")

    dst_s = ht_copy.build_session(job["dst_cookie"])
    results = ht_copy.copy_job_titles(dst_s, job["dst_instance"], selected_names)

    return render_template("index.html", step="results",
                           job_id=job_id,
                           src_instance=job["src_instance"],
                           dst_instance=job["dst_instance"],
                           results=results,
                           result_type="job_titles")


# ---------------------------------------------------------------------------
# Licenses path — select (fetch + diff)
# ---------------------------------------------------------------------------

@app.get("/licenses/<job_id>")
def select_licenses(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return _expired()

    if "licenses_unique" not in job:
        src_s = ht_copy.build_session(job["src_cookie"])
        dst_s = ht_copy.build_session(job["dst_cookie"])
        try:
            _, unique = ht_copy.fetch_licenses_with_diff(
                src_s, dst_s, job["src_instance"], job["dst_instance"]
            )
            job["licenses_unique"] = unique
        except Exception as exc:
            return render_template("index.html", step="home", job_id=job_id,
                                   src_instance=job["src_instance"],
                                   dst_instance=job["dst_instance"],
                                   error=f"Could not load Licenses: {exc}")

    return render_template("index.html", step="select_licenses", job_id=job_id,
                           src_instance=job["src_instance"],
                           dst_instance=job["dst_instance"],
                           licenses_unique=job["licenses_unique"])


# ---------------------------------------------------------------------------
# Licenses path — copy
# ---------------------------------------------------------------------------

@app.post("/copy-licenses/<job_id>")
def run_copy_licenses(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return _expired()

    selected_ids = request.form.getlist("license_ids")
    if not selected_ids:
        return render_template("index.html", step="select_licenses", job_id=job_id,
                               src_instance=job["src_instance"],
                               dst_instance=job["dst_instance"],
                               licenses_unique=job.get("licenses_unique", []),
                               error="Select at least one license.")

    selected_items = [
        item for item in job.get("licenses_unique", [])
        if item["id"] in selected_ids
    ]

    src_s = ht_copy.build_session(job["src_cookie"])
    dst_s = ht_copy.build_session(job["dst_cookie"])
    results = ht_copy.copy_licenses(
        src_s, job["src_instance"],
        dst_s, job["dst_instance"],
        selected_items,
    )

    return render_template("index.html", step="results",
                           job_id=job_id,
                           src_instance=job["src_instance"],
                           dst_instance=job["dst_instance"],
                           results=results,
                           result_type="licenses")


# ---------------------------------------------------------------------------
# Meeting Types path — select (fetch + diff)
# ---------------------------------------------------------------------------

@app.get("/meeting-types/<job_id>")
def select_meeting_types(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return _expired()

    if "meeting_types_unique" not in job:
        src_s = ht_copy.build_session(job["src_cookie"])
        dst_s = ht_copy.build_session(job["dst_cookie"])
        try:
            _, unique = ht_copy.fetch_meeting_types_with_diff(
                src_s, dst_s, job["src_instance"], job["dst_instance"]
            )
            job["meeting_types_unique"] = unique
        except Exception as exc:
            return render_template("index.html", step="home", job_id=job_id,
                                   src_instance=job["src_instance"],
                                   dst_instance=job["dst_instance"],
                                   error=f"Could not load Meeting Types: {exc}")

    return render_template("index.html", step="select_meeting_types", job_id=job_id,
                           src_instance=job["src_instance"],
                           dst_instance=job["dst_instance"],
                           meeting_types_unique=job["meeting_types_unique"])


# ---------------------------------------------------------------------------
# Meeting Types path — copy
# ---------------------------------------------------------------------------

@app.post("/copy-meeting-types/<job_id>")
def run_copy_meeting_types(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return _expired()

    selected_ids = request.form.getlist("meeting_type_ids")
    if not selected_ids:
        return render_template("index.html", step="select_meeting_types", job_id=job_id,
                               src_instance=job["src_instance"],
                               dst_instance=job["dst_instance"],
                               meeting_types_unique=job.get("meeting_types_unique", []),
                               error="Select at least one meeting type.")

    selected_items = [
        item for item in job.get("meeting_types_unique", [])
        if item["id"] in selected_ids
    ]

    src_s = ht_copy.build_session(job["src_cookie"])
    dst_s = ht_copy.build_session(job["dst_cookie"])
    results = ht_copy.copy_meeting_types(
        src_s, job["src_instance"],
        dst_s, job["dst_instance"],
        selected_items,
    )

    return render_template("index.html", step="results",
                           job_id=job_id,
                           src_instance=job["src_instance"],
                           dst_instance=job["dst_instance"],
                           results=results,
                           result_type="meeting_types")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
