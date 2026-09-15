"""Google Earth Engine authentication.

Per ProjectPlan.docx, scheduled/unattended runs use a service account; local development
uses the interactive credentials persisted by the `earthengine` CLI (`earthengine authenticate`,
run once per machine).
"""

from __future__ import annotations

import os

import ee


def initialize(project: str | None = None) -> None:
    """Initialize the Earth Engine client.

    Uses a service account when GEE_SERVICE_ACCOUNT_EMAIL and GEE_SERVICE_ACCOUNT_KEY_PATH
    are both set in the environment (the unattended/scheduled path). Otherwise falls back to
    whatever user credentials `earthengine authenticate` has already persisted on this
    machine, prompting for interactive auth only if none are found.

    project: Google Cloud project ID to bill/associate Earth Engine requests to. Falls back
    to the GEE_PROJECT environment variable. Required by current Earth Engine API versions.
    """
    project = project or os.environ.get("GEE_PROJECT")
    service_account = os.environ.get("GEE_SERVICE_ACCOUNT_EMAIL")
    key_path = os.environ.get("GEE_SERVICE_ACCOUNT_KEY_PATH")

    if service_account and key_path:
        credentials = ee.ServiceAccountCredentials(service_account, key_path)
        ee.Initialize(credentials, project=project)
        return

    try:
        ee.Initialize(project=project)
    except Exception:
        ee.Authenticate()
        ee.Initialize(project=project)
