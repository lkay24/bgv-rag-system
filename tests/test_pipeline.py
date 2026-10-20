import os
import shutil

import pymupdf
import pytest

from backend import models
from backend.agents import classifier, pipeline
from backend.services import drive_service, file_utils, gmail_service


# ---------- helpers ----------

def make_pdf(path, pages):
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_text((72, 72), text)
    doc.save(path)
    doc.close()
    return str(path)


def fake_classify(text):
    t = text.lower()
    table = {
        "aadhaar": ("identity", "aadhar_card"),
        "income tax": ("identity", "pan_card"),
        "offer of employment": ("employment", "offer_letter"),
    }
    for key, (cat, typ) in table.items():
        if key in t:
            conf = "low" if "blurry" in t else "high"
            return {"category": cat, "doc_type": typ, "confidence": conf, "reasoning": ""}
    return {"category": "unknown", "doc_type": "unknown", "confidence": "low", "reasoning": ""}


@pytest.fixture(autouse=True)
def stub_llm(monkeypatch):
    monkeypatch.setattr(classifier, "classify_document", fake_classify)


class FakeDrive:
    def __init__(self):
        self.files = {}      # id -> (name, folder_id)
        self.deleted = []
        self.fail_next_upload = False

    def upload(self, service, path, name, folder_id):
        if self.fail_next_upload:
            self.fail_next_upload = False
            raise RuntimeError("drive down")
        fid = f"f{len(self.files) + len(self.deleted) + 1}"
        self.files[fid] = (name, folder_id)
        return {"file_id": fid, "filename": name, "web_link": ""}

    def delete(self, service, fid):
        self.files.pop(fid, None)
        self.deleted.append(fid)
        return True


@pytest.fixture
def env(monkeypatch, tmp_path, db):
    drive = FakeDrive()
    inbox = {}   # candidate email -> list of pdf paths to "download"

    monkeypatch.setattr(pipeline, "get_gmail_service", lambda e, d: object())
    monkeypatch.setattr(pipeline, "get_drive_service", lambda e, d: object())
    monkeypatch.setattr(pipeline, "get_main_bgv_folder", lambda s, name: f"root:{name}")
    monkeypatch.setattr(
        pipeline, "create_candidate_folder_structure",
        lambda s, cid, name, parent, fmt: drive_service.create_candidate_folder_structure(FakeFolders(), cid, name, parent, fmt),
    )
    monkeypatch.setattr(pipeline, "upload_file_to_drive", drive.upload)
    monkeypatch.setattr(pipeline, "delete_file_from_drive", drive.delete)
    monkeypatch.setattr(pipeline, "find_candidate_files_in_source_drive", lambda s, e, f: [])

    def fake_docs(service, email, cid, workdir):
        out = []
        for i, src in enumerate(inbox.get(email, [])):
            dst = os.path.join(workdir, f"{i}_{os.path.basename(src)}")
            shutil.copy(src, dst)
            out.append(dst)
        return out

    monkeypatch.setattr(pipeline, "get_candidate_documents", fake_docs)
    monkeypatch.setattr(file_utils, "WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setattr(pipeline, "candidate_workdir", lambda cid: str(tmp_path / "work" / cid))

    db.add(models.User(email="hr@x.com", name="HR", access_token="t", refresh_token="r"))
    db.add(models.Candidate(candidate_id="C1", name="Asha Rao", email="asha@x.com", status="pending"))
    db.commit()
    return type("Env", (), {"drive": drive, "inbox": inbox, "tmp": tmp_path})


class FakeFolders:
    """Just enough of the Drive API surface for get_or_create_folder."""
    def __init__(self):
        self.made = {}

    def files(self):
        return self

    def list(self, q, fields):
        name = q.split("name='")[1].split("'")[0]
        parent = q.split(" and '")[1].split("'")[0] if " in parents" in q else None
        fid = self.made.get((name, parent))
        outer = self
        class R:
            def execute(self_inner):
                return {"files": [{"id": fid, "name": name}] if fid else []}
        return R()

    def create(self, body, fields):
        key = (body["name"], (body.get("parents") or [None])[0])
        fid = f"folder:{body['name']}"
        self.made[key] = fid
        class R:
            def execute(self_inner):
                return {"id": fid, "name": body["name"]}
        return R()


# ---------- classifier units ----------

def test_normalize_classification_enforces_taxonomy():
    n = classifier.normalize_classification
    # the model says "address" for an Aadhaar; the fixed tie-break wins
    assert n({"doc_type": "Aadhar Card", "category": "address", "confidence": "HIGH"})["category"] == "identity"
    assert n({"doc_type": "aadhar_card", "confidence": "certain"})["confidence"] == "low"
    assert n({"doc_type": "made_up_doc", "category": "identity", "confidence": "high"})["doc_type"] == "unknown"
    assert n({})["doc_type"] == "unknown"


def test_parse_model_json_variants():
    p = classifier.parse_model_json
    assert p('{"a": 1}') == {"a": 1}
    assert p('```json\n{"a": 1}\n```') == {"a": 1}
    assert p('Sure! {"a": 1} hope that helps') == {"a": 1}
    with pytest.raises(ValueError):
        p("nope")


def page(t, c="high", cat="identity", path=None):
    return {"path": path or f"p-{t}-{id(object())}", "doc_type": t, "category": cat, "confidence": c}


def test_group_pages_keeps_every_page():
    a1, pan, a2 = page("aadhar_card", "medium"), page("pan_card"), page("aadhar_card", "high")
    unk = page("unknown", "low", "unknown")
    groups = classifier.group_pages_by_document([a1, pan, unk, a2])
    by_type = {g["doc_type"]: g for g in groups}
    assert [g["doc_type"] for g in groups] == ["aadhar_card", "pan_card"]       # first-seen order
    assert by_type["aadhar_card"]["pages"] == [a1["path"], a2["path"]]          # non-adjacent pages merged
    assert by_type["aadhar_card"]["confidence"] == "high"                       # best page wins
    assert by_type["pan_card"]["pages"] == [pan["path"], unk["path"]]           # unknown page = continuation

    lead = classifier.group_pages_by_document([unk, a1])
    assert len(lead) == 1 and lead[0]["pages"] == [unk["path"], a1["path"]]
    allunk = classifier.group_pages_by_document([unk, page("unknown", "low", "unknown")])
    assert len(allunk) == 1 and allunk[0]["doc_type"] == "unknown"


def test_detect_and_split_combined_pdf(tmp_path):
    src = make_pdf(tmp_path / "combined.pdf", [
        "Aadhaar front UIDAI", "Aadhaar back address", "Income Tax Department PAN",
    ])
    # page 2 has text that our fake can't classify? make it unknown on purpose
    docs = classifier.detect_and_split_documents(src, str(tmp_path / "split"))
    assert [d["doc_type"] for d in docs] == ["aadhar_card", "pan_card"]
    assert pymupdf.open(docs[0]["path"]).page_count == 2    # back page is not lost
    assert pymupdf.open(docs[1]["path"]).page_count == 1


def test_single_document_multipage_keeps_original(tmp_path):
    src = make_pdf(tmp_path / "offer.pdf", ["Offer of Employment p1", "Offer of Employment p2"])
    docs = classifier.detect_and_split_documents(src, str(tmp_path / "split"))
    assert len(docs) == 1 and docs[0]["path"] == src


def test_ocr_reads_scanned_and_rotated_pages(tmp_path):
    pytest.importorskip("pytesseract")
    if not shutil.which("tesseract"):
        pytest.skip("tesseract not installed")
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (1600, 500), "white")
    font = ImageFont.load_default(size=70)
    ImageDraw.Draw(img).text((40, 150), "GOVERNMENT OF INDIA PERMANENT ACCOUNT NUMBER", fill="black", font=font)
    for angle, name in ((0, "up"), (180, "flipped"), (90, "side")):
        png = tmp_path / f"{name}.png"
        img.rotate(angle, expand=True).save(png)
        pdf = file_utils.image_to_pdf(str(png), str(tmp_path / f"{name}.pdf"))
        text = classifier.extract_text_from_pdf(pdf).upper()
        assert "PERMANENT" in text and "ACCOUNT" in text, (name, text[:80])


# ---------- pipeline ----------

def test_pipeline_end_to_end_and_status(env, db):
    env.inbox["asha@x.com"] = [
        make_pdf(env.tmp / "combo.pdf", ["Aadhaar front", "Aadhaar back", "Income Tax PAN"]),
    ]
    db.add(models.Settings(key="required_documents", value=["aadhar_card", "pan_card"]))
    db.commit()

    res = pipeline.process_candidate("C1", "hr@x.com", db)
    assert res["success"] and res["status"] == "completed", res
    docs = {d.doc_type: d for d in db.query(models.Document)}
    assert set(docs) == {"aadhar_card", "pan_card"}
    # configured default format -> <C1-Asha_Rao-date>/Identity/aadhar_card.pdf
    assert docs["aadhar_card"].drive_path.startswith("C1-Asha_Rao-")
    assert docs["aadhar_card"].drive_path.endswith("/Identity/aadhar_card.pdf")
    # temp files are cleaned
    assert not (env.tmp / "work" / "C1").exists()


def test_custom_folder_format_is_used(env, db):
    env.inbox["asha@x.com"] = [make_pdf(env.tmp / "a.pdf", ["Aadhaar card issued by UIDAI"])]
    db.add(models.Settings(key="folder_name_format", value="{name}__{candidate_id}"))
    db.commit()
    pipeline.process_candidate("C1", "hr@x.com", db)
    assert db.query(models.Document).one().drive_path == "Asha_Rao__C1/Identity/aadhar_card.pdf"


def test_resend_replaces_only_when_better_and_upload_first(env, db):
    env.inbox["asha@x.com"] = [make_pdf(env.tmp / "a.pdf", ["Aadhaar card UIDAI blurry"])]
    pipeline.process_candidate("C1", "hr@x.com", db)
    old = db.query(models.Document).one()
    assert old.ai_confidence == "low"

    # better copy arrives, but Drive upload fails -> old file must survive
    env.inbox["asha@x.com"] = [make_pdf(env.tmp / "b.pdf", ["Aadhaar card UIDAI clear"])]
    env.drive.fail_next_upload = True
    res = pipeline.process_candidate("C1", "hr@x.com", db)
    assert res["documents_processed"][0]["status"] == "failed"
    assert db.query(models.Document).one().drive_file_id == old.drive_file_id
    assert old.drive_file_id in env.drive.files and not env.drive.deleted

    res = pipeline.process_candidate("C1", "hr@x.com", db)
    new = db.query(models.Document).one()
    assert new.ai_confidence == "high" and new.drive_file_id != old.drive_file_id
    assert env.drive.deleted == [old.drive_file_id]

    # equal confidence -> skipped duplicate
    res = pipeline.process_candidate("C1", "hr@x.com", db)
    assert res["documents_processed"][0]["status"] == "skipped"


def test_recheck_with_no_new_mail_keeps_status(env, db):
    env.inbox["asha@x.com"] = [make_pdf(env.tmp / "a.pdf", ["Aadhaar card issued by UIDAI"])]
    pipeline.process_candidate("C1", "hr@x.com", db)
    assert db.query(models.Candidate).one().status == "partial"
    env.inbox["asha@x.com"] = []
    res = pipeline.process_candidate("C1", "hr@x.com", db)
    assert res["success"] and res["status"] == "partial"      # was wrongly flipped to failed before


def test_no_documents_at_all_is_failed(env, db):
    res = pipeline.process_candidate("C1", "hr@x.com", db)
    assert not res["success"] and db.query(models.Candidate).one().status == "failed"


def test_connection_problem_does_not_blame_candidate(env, db, monkeypatch):
    from backend.services.google_auth import GoogleAuthError
    def boom(e, d):
        raise GoogleAuthError("token revoked")
    monkeypatch.setattr(pipeline, "get_gmail_service", boom)
    res = pipeline.process_candidate("C1", "hr@x.com", db)
    assert not res["success"] and res["auth_error"]
    assert db.query(models.Candidate).one().status == "pending"   # not in the review queue


def test_batch_aborts_on_auth_failure_and_leaves_rest_pending(env, db, monkeypatch):
    from backend.services.google_auth import GoogleAuthError
    db.add(models.Batch(id=1, filename="b.xlsx"))
    db.query(models.Candidate).update({"batch_id": 1})
    db.add(models.Candidate(candidate_id="C2", name="B", email="b@x.com", status="pending", batch_id=1))
    db.commit()
    def boom(e, d):
        raise GoogleAuthError("token revoked")
    monkeypatch.setattr(pipeline, "get_gmail_service", boom)
    res = pipeline.process_batch(1, "hr@x.com", db)
    assert not res["success"] and res["total_processed"] == 1
    assert {c.status for c in db.query(models.Candidate)} == {"pending"}


# ---------- services ----------

def test_gmail_download_is_safe_and_complete(tmp_path):
    import base64
    def b64(b): return base64.urlsafe_b64encode(b).decode().rstrip("=")
    pdf = open(make_pdf(tmp_path / "s.pdf", ["x"]), "rb").read()
    png_doc = pymupdf.open(); pg = png_doc.new_page(); pg.insert_text((50, 50), "hi")
    png = pg.get_pixmap().tobytes("png")
    message = {"payload": {"parts": [
        {"filename": "", "parts": [   # nested multipart
            {"filename": "../../etc/evil.pdf", "body": {"attachmentId": "A1"}},
            {"filename": "photo.PNG", "body": {"data": b64(png)}},
        ]},
        {"filename": "notes.docx", "body": {"attachmentId": "A2"}},
    ]}}
    class Svc:
        def users(self): return self
        def messages(self): return self
        def attachments(self): return self
        def get(self, **kw):
            data = {"A1": pdf}.get(kw.get("id"))
            return type("R", (), {"execute": lambda s: message if data is None else {"data": b64(data)}})()
    out = gmail_service.download_attachments(Svc(), "m1", str(tmp_path / "dl"))
    assert len(out) == 2 and all(p.endswith(".pdf") for p in out)
    assert all(os.path.dirname(p) == str(tmp_path / "dl") for p in out)    # no path traversal
    assert all(pymupdf.open(p).page_count == 1 for p in out)               # image became a PDF


def test_query_injection_is_neutralised():
    assert file_utils.drive_escape("a'b\\c") == "a\\'b\\\\c"
    class Boom:
        def users(self): raise AssertionError("must not search")
    assert gmail_service.search_candidate_emails(Boom(), "x@y.com OR from:ceo@corp.com") == []
    assert file_utils.safe_name("../../a b/c.pdf") == "c.pdf"
    assert file_utils.safe_name("..") == "file"


def test_folder_format_validation_and_rendering():
    v = drive_service.validate_folder_format
    assert v("{candidate_id}-{name}-{date}") == ""
    assert "candidate_id" in v("{name}")
    assert v("{x}") and v("")
    r = drive_service.build_candidate_folder_name("{candidate_id}/{name}", "C9", " Ann  Lee ")
    assert r == "C9-Ann_Lee"
    assert drive_service.build_candidate_folder_name("bad", "C9", "A").startswith("C9-A-")


def test_ocr_large_upside_down_photo_is_scaled_sensibly(tmp_path):
    """A phone-sized photo must not become a giant PDF page that tesseract fragments."""
    if not shutil.which("tesseract"):
        pytest.skip("tesseract not installed")
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (1700, 600), "white")
    ImageDraw.Draw(img).text((40, 200), "Income Tax Department PAN permanent account", fill="black",
                             font=ImageFont.load_default(size=70))
    png = tmp_path / "photo.png"
    img.rotate(180).save(png)
    pdf = file_utils.image_to_pdf(str(png), str(tmp_path / "photo.pdf"))
    assert max(pymupdf.open(pdf)[0].rect.width, pymupdf.open(pdf)[0].rect.height) <= 842.5
    text = classifier.extract_text_from_pdf(pdf)
    assert "Income Tax Department" in text and "permanent account" in text
