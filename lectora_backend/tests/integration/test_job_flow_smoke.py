"""Integration smoke test for API -> DB -> Blob -> Service Bus -> Worker flow.

Prerequisites:
- API is running: uvicorn lectora_backend.main:app --reload
- Worker is running: python -m lectora_backend.worker
- .env is configured
- DB migrations are applied
"""
import time

import requests

from lectora_backend.core.state_manager import StateManager


API_BASE_URL = "http://127.0.0.1:8000"


PAYLOAD = {
    "courseTitle": "Introduction to Compliance",
    "courseType": "compliance",
    "inputs": {
        "courseBrief": {"blobPath": "uploaded-documents/j-1/course_brief.docx"},
        "timedOutline": {"blobPath": "uploaded-documents/j-1/timed_outline.docx"},
        "studyGuide": {"blobPath": "uploaded-documents/j-1/study_guide.docx"},
        "examReference": None,
        "complianceNotes": None,
    },
}


def test_job_flow_smoke() -> None:
    response = requests.post(f"{API_BASE_URL}/jobs", json=PAYLOAD, timeout=20)
    assert response.status_code == 202, response.text

    job_id = response.json()["jobId"]
    print(f"Created job: {job_id}")

    latest_job = None
    for _ in range(20):
        status_response = requests.get(f"{API_BASE_URL}/jobs/{job_id}", timeout=20)
        assert status_response.status_code == 200, status_response.text

        latest_job = status_response.json()
        a0_stage = next(stage for stage in latest_job["stages"] if stage["stage"] == "A0")

        if latest_job["status"] == "PROCESSING" and a0_stage["status"] == "COMPLETED":
            print("SQL metadata updated: job PROCESSING, A0 COMPLETED")
            break

        time.sleep(1)
    else:
        raise AssertionError(f"A0 did not complete in time. Last job state: {latest_job}")

    state = StateManager().load(job_id)
    a0_state = state["stageExecutionState"]["A0"]

    assert a0_state["status"] == "COMPLETED"
    assert a0_state.get("startedAt")
    assert a0_state.get("completedAt")
    print(f"Blob shared state A0: {a0_state}")
    print("PASS: API -> DB -> Blob -> Service Bus -> Worker smoke flow verified")
