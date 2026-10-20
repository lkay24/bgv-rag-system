import io

import pandas as pd

from backend import models


def make_xlsx(rows, columns=("candidateId", "name", "email", "phone")):
    buf = io.BytesIO()
    pd.DataFrame(rows, columns=list(columns)).to_excel(buf, index=False)
    return buf.getvalue()


def upload(client, rows, filename="c.xlsx", **kw):
    return client.post(
        "/api/candidates/upload",
        files={"file": (filename, make_xlsx(rows, **kw), "application/octet-stream")},
    )


def test_upload_handles_duplicates_blanks_and_numeric_ids(client):
    r = upload(client, [
        ["C1", "Asha Rao", "ASHA@x.com", None],
        ["C1", "Asha Again", "asha2@x.com", "1"],   # repeated in sheet
        [2, "Bo Li", "bo@x.com", 9800000000],        # numeric id / phone
        ["C3", "No Email", None, "5"],               # invalid
        [None, None, None, None],                    # blank row ignored
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["candidates_added"] == 2
    assert body["candidates_skipped"] == 1
    assert body["invalid_count"] == 1
    assert body["invalid_rows"][0]["row"] == 5

    cands = {c["candidate_id"]: c for c in client.get("/api/candidates").json()["candidates"]}
    assert set(cands) == {"C1", "2"}
    assert cands["C1"]["email"] == "asha@x.com"
    assert cands["C1"]["phone"] is None          # not the string "nan"
    assert cands["2"]["phone"] == "9800000000"   # not "9800000000.0"

    batch = client.get("/api/batches").json()["batches"][0]
    assert batch["total_candidates"] == 2


def test_upload_rejects_bad_input_without_creating_batch(client):
    assert client.post("/api/candidates/upload", files={"file": ("x.txt", b"hi")}).status_code == 400
    assert client.post("/api/candidates/upload", files={"file": ("x.xlsx", b"not excel")}).status_code == 400
    r = upload(client, [["C1", "A", "a@x.com", "1"]], columns=("candidateId", "name", "email", "mobile"))
    assert r.status_code == 400 and "phone" in r.json()["detail"]
    # all rows already exist -> explicit error, no empty batch
    assert upload(client, [["C1", "A", "a@x.com", "1"]]).status_code == 200
    assert upload(client, [["C1", "A", "a@x.com", "1"]]).status_code == 400
    assert client.get("/api/batches").json()["total_batches"] == 1


def test_upload_header_case_insensitive(client):
    r = upload(client, [["C1", "A", "a@x.com", "1"]], columns=("CandidateID", " Name", "EMAIL", "Phone"))
    assert r.status_code == 200, r.text


def test_dashboard_and_counts(client, db):
    upload(client, [["C1", "A", "a@x.com", "1"], ["C2", "B", "b@x.com", "2"]])
    db.query(models.Candidate).filter_by(candidate_id="C1").update({"status": "completed"})
    db.commit()
    d = client.get("/api/candidates/dashboard").json()
    assert d["total_candidates"] == 2 and d["fully_verified"] == 1 and d["pending"] == 1
    b = client.get("/api/batches").json()["batches"][0]["status_breakdown"]
    assert b["completed"] == 1 and b["pending"] == 1
    s = client.get("/api/candidates/batch/1/status").json()
    assert s["progress_percent"] == 50.0


def test_settings_validation(client):
    r = client.post("/api/settings/required-documents", json={"required_documents": ["PAN Card", "pan_card", "aadhar_card"]})
    assert r.json()["required_documents"] == ["pan_card", "aadhar_card"]
    r = client.post("/api/settings/required-documents", json={"required_documents": ["spaceship_licence"]})
    assert r.status_code == 400 and "spaceship_licence" in r.json()["detail"]

    ok = client.post("/api/settings/folder-config", json={"folder_name_format": "{candidate_id}_{name}", "destination_folder_name": "Docs"})
    assert ok.status_code == 200
    for bad in ["{unknown}", "static-name", "", "{candidate_id"]:
        r = client.post("/api/settings/folder-config", json={"folder_name_format": bad, "destination_folder_name": "Docs"})
        assert r.status_code == 400, bad
    s = client.get("/api/settings").json()
    assert s["folder_name_format"] == "{candidate_id}_{name}" and s["destination_folder_name"] == "Docs"


def test_required_docs_excel_upload(client):
    buf = io.BytesIO()
    pd.DataFrame({"doc_type": ["pan_card", "Passport", None]}).to_excel(buf, index=False)
    r = client.post("/api/settings/required-documents/upload", files={"file": ("r.xlsx", buf.getvalue())})
    assert r.json()["required_documents"] == ["pan_card", "passport"]
    # headerless sheet: first value must not be eaten as the header
    buf = io.BytesIO()
    pd.DataFrame(["pan_card", "voter_id"]).to_excel(buf, index=False, header=False)
    r = client.post("/api/settings/required-documents/upload", files={"file": ("r.xlsx", buf.getvalue())})
    assert r.json()["required_documents"] == ["pan_card", "voter_id"]


def test_process_endpoints_guard_rails(client, db):
    upload(client, [["C1", "A", "a@x.com", "1"]])
    assert client.post("/api/candidates/NOPE/process").status_code == 404
    assert client.post("/api/candidates/C1/retry").status_code == 400   # not failed
    # no Google account connected -> refuse before queueing background work
    r = client.post("/api/candidates/batch/1/process")
    assert r.status_code == 400 and "Google" in r.json()["detail"]
    assert client.get("/api/candidates/batch/1/status").json()["processing_status"] == "not_started"
    assert client.post("/api/candidates/batch/99/process").status_code == 404
    db.query(models.Batch).update({"processing_status": "processing"})
    db.commit()
    assert client.post("/api/candidates/batch/1/process").status_code == 409


def test_auth_flow_uses_state_and_never_leaks_tokens(client, db):
    r = client.get("/api/auth/google", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert "state=" in r.headers["location"] and "redirect_uri=http%3A%2F%2Flocalhost" in r.headers["location"]
    # forged callback without a valid state is rejected
    assert client.get("/api/auth/callback?code=x&state=bogus", follow_redirects=False).status_code == 400
    assert client.get("/api/auth/status").json() == {"connected": False, "email": None, "name": None}
    db.add(models.User(email="hr@x.com", name="HR", access_token="SECRET", refresh_token="SECRET2"))
    db.commit()
    body = client.get("/api/auth/status").json()
    assert body["connected"] and body["email"] == "hr@x.com"
    assert "SECRET" not in client.get("/api/auth/status").text


def test_frontend_is_served(client):
    assert "BGV" in client.get("/").text
    assert client.get("/health").json() == {"status": "ok"}


def test_oauth_callback_creates_user_and_redirects(client, db, monkeypatch):
    from urllib.parse import parse_qs, urlparse
    from backend.api import auth

    class Resp:
        def __init__(self, data): self._d = data
        def json(self): return self._d

    monkeypatch.setattr(auth.requests, "post", lambda url, **kw: Resp({"access_token": "AT", "refresh_token": "RT"}))
    monkeypatch.setattr(auth.requests, "get", lambda url, **kw: Resp({"email": "hr@x.com", "name": "HR"}))

    login = client.get("/api/auth/google", follow_redirects=False)
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]

    r = client.get(f"/api/auth/callback?code=abc&state={state}", follow_redirects=False)
    assert r.status_code in (302, 307) and "settings.html?connected=hr%40x.com" in r.headers["location"]
    user = db.query(models.User).one()
    assert (user.email, user.refresh_token) == ("hr@x.com", "RT")
    assert client.get("/api/auth/status").json()["email"] == "hr@x.com"
    # a state can only be used once
    assert client.get(f"/api/auth/callback?code=abc&state={state}", follow_redirects=False).status_code == 400
    # user declined on Google's consent screen
    r = client.get("/api/auth/callback?error=access_denied", follow_redirects=False)
    assert "auth_error=access_denied" in r.headers["location"]


def test_startup_releases_work_stuck_in_processing(client, db):
    from backend.main import recover_interrupted_work
    db.add(models.Batch(id=1, filename="b", processing_status="processing"))
    db.add(models.Candidate(candidate_id="C1", name="A", email="a@x.com", status="processing", batch_id=1))
    db.commit()
    recover_interrupted_work()
    db.expire_all()
    assert db.query(models.Candidate).one().status == "pending"
    assert db.query(models.Batch).one().processing_status == "failed"


def test_revoked_google_token_gives_actionable_error(db, monkeypatch):
    from google.auth.exceptions import RefreshError
    from backend.services import google_auth

    db.add(models.User(email="hr@x.com", name="HR", access_token="old", refresh_token="revoked"))
    db.commit()
    google_auth._last_refresh.clear()

    def refuse(self, request): raise RefreshError("invalid_grant")
    monkeypatch.setattr(google_auth.Credentials, "refresh", refuse)
    import pytest
    with pytest.raises(google_auth.GoogleAuthError, match="reconnect"):
        google_auth.get_credentials("hr@x.com", db)
    with pytest.raises(google_auth.GoogleAuthError, match="log in"):
        google_auth.get_credentials("nobody@x.com", db)
