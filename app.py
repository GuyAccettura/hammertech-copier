"""
Flask web app for HammerTech checklist copier.

3-step flow:
  1. GET  /          → login form (instances + credentials)
  2. POST /auth      → Playwright login + fetch checklists → /select/<job>
  3. GET  /select/<job> → checklist selection form
  4. POST /copy/<job>   → run copy → results page
"""

import os
import uuid
from typing import Any, Dict

from flask import Flask, render_template, request, redirect, url_for

import auth as ht_auth
import copier as ht_copy

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")

# In-memory job store. Fine for a single-worker deployment.
# Keys: job_id (str) → job dict
_jobs: Dict[str, Dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Step 1 — login form
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return render_template("index.html", step="login", error=None)


# ---------------------------------------------------------------------------
# Step 2 — authenticate both instances, fetch checklists
# ---------------------------------------------------------------------------

@app.post("/auth")
def authenticate():
    src_instance = (request.form.get("src_instance") or "").strip()
    dst_instance = (request.form.get("dst_instance") or "").strip()
    email = (request.form.get("email") or "").strip()
    password = (request.form.get("password") or "").strip()

    if not all([src_instance, dst_instance, email, password]):
        return render_template(
            "index.html", step="login",
            error="All fields are required.",
        )

    # --- Playwright login (two instances) ---
    try:
        src_cookie = ht_auth.get_auth_cookie_playwright(src_instance, email, password)
        print(f"[DEBUG] SRC cookie names: {[p.split('=')[0] for p in src_cookie.split('; ')]}")
    except Exception as exc:
        return render_template(
            "index.html", step="login",
            error=f"Login failed for SOURCE '{src_instance}': {exc}",
        )

    try:
        dst_cookie = ht_auth.get_auth_cookie_playwright(dst_instance, email, password)
        print(f"[DEBUG] DST cookie names: {[p.split('=')[0] for p in dst_cookie.split('; ')]}")
    except Exception as exc:
        return render_template(
            "index.html", step="login",
            error=f"Login failed for DESTINATION '{dst_instance}': {exc}",
        )

    # --- Fetch issue-type maps via dev API (bearer token, no browser needed) ---
    try:
        src_id_to_name, _ = ht_copy.build_issue_type_maps_via_dev_api(
            src_instance, email, password
        )
        _, dst_name_to_id = ht_copy.build_issue_type_maps_via_dev_api(
            dst_instance, email, password
        )
    except Exception as exc:
        # Non-fatal — fall back to empty maps (IDs won't be remapped)
        src_id_to_name = {}
        dst_name_to_id = {}
        issue_type_warning = f"Could not load Issue Types (defaultIssueTypeId will be cleared): {exc}"
    else:
        issue_type_warning = None

    # --- Fetch checklist list from source ---
    src_s = ht_copy.build_session(src_cookie)
    try:
        checklists = ht_copy.fetch_checklists(src_s, src_instance)
    except Exception as exc:
        return render_template(
            "index.html", step="login",
            error=f"Fetched cookies OK, but could not load checklists from '{src_instance}': {exc}",
        )

    job_id = str(uuid.uuid4())
    _jobs[job_id] = {
        "src_instance": src_instance,
        "dst_instance": dst_instance,
        "src_cookie": src_cookie,
        "dst_cookie": dst_cookie,
        "src_id_to_name": src_id_to_name,
        "dst_name_to_id": dst_name_to_id,
        "checklists": checklists,
        "issue_type_warning": issue_type_warning,
    }
    return redirect(url_for("select_checklists", job_id=job_id))


# ---------------------------------------------------------------------------
# Step 3 — checklist selection
# ---------------------------------------------------------------------------

@app.get("/select/<job_id>")
def select_checklists(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return render_template("index.html", step="login", error="Session expired. Please log in again.")

    return render_template(
        "index.html",
        step="select",
        job_id=job_id,
        src_instance=job["src_instance"],
        dst_instance=job["dst_instance"],
        checklists=job["checklists"],
        issue_type_warning=job.get("issue_type_warning"),
    )


# ---------------------------------------------------------------------------
# Step 4 — run copy, show results
# ---------------------------------------------------------------------------

@app.post("/copy/<job_id>")
def run_copy(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return render_template("index.html", step="login", error="Session expired. Please log in again.")

    selected_ids = request.form.getlist("checklist_ids")
    if not selected_ids:
        return render_template(
            "index.html",
            step="select",
            job_id=job_id,
            src_instance=job["src_instance"],
            dst_instance=job["dst_instance"],
            checklists=job["checklists"],
            issue_type_warning=job.get("issue_type_warning"),
            error="Select at least one checklist.",
        )

    results = ht_copy.copy_checklists(
        src_instance=job["src_instance"],
        dst_instance=job["dst_instance"],
        src_cookie=job["src_cookie"],
        dst_cookie=job["dst_cookie"],
        checklist_ids=selected_ids,
        src_id_to_name=job["src_id_to_name"],
        dst_name_to_id=job["dst_name_to_id"],
    )

    # Clean up job after use
    _jobs.pop(job_id, None)

    return render_template(
        "index.html",
        step="results",
        src_instance=job["src_instance"],
        dst_instance=job["dst_instance"],
        results=results,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
