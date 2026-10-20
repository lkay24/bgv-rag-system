import io
import os
import json
import pymupdf as fitz
import pytesseract
from PIL import Image, ImageFilter, ImageOps
from groq import Groq
from dotenv import load_dotenv

from backend.taxonomy import ALL_DOC_TYPES, PRIMARY_CATEGORY, prompt_listing

load_dotenv()

CONFIDENCE_LEVELS = ("low", "medium", "high")
CONFIDENCE_RANK = {level: i for i, level in enumerate(CONFIDENCE_LEVELS)}

_client = None


def get_client() -> Groq:
    """Create the Groq client on first use so the app can start (and be tested) without a key."""
    global _client
    if _client is None:
        _client = Groq(api_key=os.getenv("GROQ_API_KEY"), max_retries=5, timeout=60)
    return _client


def extract_text_from_pdf(pdf_path: str) -> str:
    try:
        with fitz.open(pdf_path) as doc:
            full_text = "".join(page.get_text() for page in doc)

        if len(full_text.strip()) > 10:
            print(f"✅ Extracted {len(full_text)} characters from {pdf_path} (text layer)")
            return full_text

        print(f"⚠️ No text layer found in {pdf_path}, trying OCR...")
        return extract_text_using_ocr(pdf_path)

    except Exception as e:
        print(f"❌ Error reading PDF {pdf_path}: {e}")
        return ""


def preprocess_image_for_ocr(image):
    image = image.convert('L')
    image = ImageOps.autocontrast(image)
    image = image.filter(ImageFilter.SHARPEN)
    return image


OSD_MIN_CONFIDENCE = 2.0   # below this tesseract's orientation guess is noise (sparse pages)
GOOD_ENOUGH_SCORE = 150    # ~ a handful of confidently-read words


def _ocr_with_score(image):
    """OCR one image; returns (text, score) where score sums the confidence of well-read words."""
    kwargs = {"output_type": pytesseract.Output.DICT}
    try:
        data = pytesseract.image_to_data(image, lang='eng+hin', **kwargs)
    except pytesseract.TesseractError:
        # Hindi language pack not installed - English-only still beats nothing.
        data = pytesseract.image_to_data(image, **kwargs)

    lines = {}
    score = 0.0
    for i, word in enumerate(data["text"]):
        word = word.strip()
        if not word:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(word)
        conf = float(data["conf"][i])
        if conf > 50:
            score += conf
    return "\n".join(" ".join(words) for words in lines.values()) + "\n", score


def _detected_rotation(image):
    """Clockwise degrees needed to straighten the page, or None if tesseract isn't sure."""
    try:
        osd = pytesseract.image_to_osd(image, output_type=pytesseract.Output.DICT)
        if float(osd.get("orientation_conf", 0)) >= OSD_MIN_CONFIDENCE:
            return int(osd.get("rotate", 0))
    except Exception:
        pass  # needs enough text to work with
    return None


def _best_rotation_ocr(image) -> str:
    """OCR a page that may have been scanned sideways or upside down.

    Confident orientation detection -> a single OCR pass. Otherwise compare orientations
    by tesseract's word confidence (text length alone would reward garbage, and upside-down
    text is still read "confidently", so 0 and 180 are always compared); sideways
    orientations are only tried when neither of those reads well.
    """
    angle = _detected_rotation(image)
    if angle is not None:
        text, _ = _ocr_with_score(image.rotate(-angle, expand=True) if angle else image)
        return text

    best_text, best_score = "", -1.0
    for group in ((0, 180), (90, 270)):
        for rotation in group:
            text, score = _ocr_with_score(image.rotate(rotation, expand=True) if rotation else image)
            if score > best_score:
                best_text, best_score = text, score
        if best_score >= GOOD_ENOUGH_SCORE:
            break
    return best_text


OCR_TARGET_PX = 2800   # tesseract reads best when body text is ~25-35px tall: A4 at ~300dpi


def _ocr_dpi(page) -> int:
    """Render size from the page's physical size so odd-sized scans don't come out huge or tiny."""
    longest_in = max(page.rect.width, page.rect.height) / 72
    return int(min(400, max(100, OCR_TARGET_PX / max(longest_in, 0.1))))


def extract_text_using_ocr(pdf_path: str) -> str:
    try:
        full_text = ""
        with fitz.open(pdf_path) as doc:
            for page in doc:
                pix = page.get_pixmap(dpi=_ocr_dpi(page))
                image = Image.open(io.BytesIO(pix.tobytes("png")))
                full_text += _best_rotation_ocr(preprocess_image_for_ocr(image))

        print(f"✅ OCR extracted {len(full_text)} characters from {pdf_path}")
        return full_text

    except Exception as e:
        print(f"❌ OCR failed for {pdf_path}: {e}")
        return ""


UNKNOWN = {
    "category": "unknown",
    "doc_type": "unknown",
    "confidence": "low",
}


def _unknown(reason: str) -> dict:
    return {**UNKNOWN, "reasoning": reason}


def normalize_classification(raw: dict) -> dict:
    """Validate the model's answer against the taxonomy.

    The model is only trusted for *which type* a document is; the category is always
    taken from the taxonomy so it can never disagree with the folder layout.
    """
    doc_type = str(raw.get("doc_type", "")).strip().lower().replace(" ", "_")

    if doc_type not in ALL_DOC_TYPES:
        return _unknown(f"Model returned unrecognised document type {doc_type!r}")

    confidence = str(raw.get("confidence", "low")).strip().lower()
    if confidence not in CONFIDENCE_RANK:
        confidence = "low"

    return {
        "category": PRIMARY_CATEGORY[doc_type],
        "doc_type": doc_type,
        "confidence": confidence,
        "reasoning": str(raw.get("reasoning", "")),
    }


def parse_model_json(content: str) -> dict:
    content = content.strip()
    if "```" in content:
        fenced = content.split("```")[1]
        content = fenced[4:] if fenced.startswith("json") else fenced
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model response")
    return json.loads(content[start:end + 1])


def classify_document(text: str) -> dict:
    if not text or len(text.strip()) < 10:
        return _unknown("Could not extract text from PDF")

    prompt = f"""
You are a document classification expert for Indian HR background verification.

I will give you text extracted from a document.
Your job is to identify what TYPE of document it is.

DOCUMENT CATEGORIES AND TYPES:
{prompt_listing()}

IMPORTANT TIE-BREAK RULE:
Some documents legitimately serve more than one purpose in real life.
For example, an Aadhaar Card can prove both identity AND address, and a
PAN Card can prove both identity AND financial details. Always answer with
the single document type; the category is assigned from the list above
(aadhar_card, pan_card and passport are always "identity").

INSTRUCTIONS:
1. Read the document text carefully
2. Identify the category and specific type (doc_type MUST be one of the types listed above)
3. Give confidence level: high, medium, or low
4. Explain your reasoning briefly

VERY IMPORTANT:
- Respond ONLY in JSON format
- No extra text before or after JSON

JSON FORMAT:
{{
    "category": "identity",
    "doc_type": "aadhar_card",
    "confidence": "high",
    "reasoning": "Contains UIDAI text and 12-digit Aadhaar number"
}}

DOCUMENT TEXT:
{text[:3000]}
"""

    try:
        response = get_client().chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {
                    "role": "system",
                    "content": "You are a document classification expert. Always respond in valid JSON format only."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            temperature=0,
            response_format={"type": "json_object"}
        )

        result = normalize_classification(parse_model_json(response.choices[0].message.content))

        print(f"🤖 AI classified: {result['category']} → {result['doc_type']} ({result['confidence']} confidence)")

        return result

    except Exception as e:
        print(f"❌ AI classification error: {e}")
        return _unknown(f"Classification failed: {e}")


def process_pdf(pdf_path: str) -> dict:
    print(f"\n📄 Processing: {pdf_path}")

    text = extract_text_from_pdf(pdf_path)
    classification = classify_document(text)

    classification['pdf_path'] = pdf_path
    classification['filename'] = os.path.basename(pdf_path)

    return classification


def get_suggested_filename(doc_type: str) -> str:
    return f"{doc_type}.pdf"


def split_pdf_into_pages(pdf_path: str, output_dir: str) -> list:
    os.makedirs(output_dir, exist_ok=True)

    doc = fitz.open(pdf_path)
    page_paths = []

    for page_num in range(len(doc)):
        single_page_doc = fitz.open()
        single_page_doc.insert_pdf(doc, from_page=page_num, to_page=page_num)

        page_path = os.path.join(output_dir, f"page_{page_num + 1}.pdf")
        single_page_doc.save(page_path)
        single_page_doc.close()

        page_paths.append(page_path)

    doc.close()

    print(f"📄 Split {pdf_path} into {len(page_paths)} individual pages")
    return page_paths


def merge_pages_into_pdf(page_paths: list, output_path: str) -> str:
    merged_doc = fitz.open()

    for page_path in page_paths:
        page_doc = fitz.open(page_path)
        merged_doc.insert_pdf(page_doc)
        page_doc.close()

    merged_doc.save(output_path)
    merged_doc.close()

    return output_path


def _classify_pdf(path: str) -> dict:
    classification = classify_document(extract_text_from_pdf(path))
    return {
        "path": path,
        "doc_type": classification["doc_type"],
        "category": classification["category"],
        "confidence": classification["confidence"],
    }


def group_pages_by_document(pages: list) -> list:
    """Group classified pages into documents, preserving page order.

    Pages of the same type are merged even when another document sits between them
    (e.g. Aadhaar front, PAN, Aadhaar back). A page we couldn't classify is treated as
    a continuation of the page before it (the back of a card or a blank/overflow page
    has little text) rather than being silently dropped; leading unclassified pages
    join the first real document. If nothing was classified, a single unknown group
    is returned.
    """
    groups = {}
    order = []
    previous_key = None
    pending_unknown = []

    for page in pages:
        key = page["doc_type"]
        if key == "unknown":
            if previous_key is None:
                pending_unknown.append(page)
                continue
            key = previous_key

        if key not in groups:
            groups[key] = []
            order.append(key)
        if pending_unknown:
            groups[key].extend(pending_unknown)
            pending_unknown = []
        groups[key].append(page)
        previous_key = key

    if pending_unknown:  # nothing at all was classified
        groups["unknown"] = pending_unknown
        order.append("unknown")

    result = []
    for key in order:
        members = sorted(groups[key], key=lambda p: pages.index(p))
        classified = [p for p in members if p["doc_type"] != "unknown"]
        best = max(classified, key=lambda p: CONFIDENCE_RANK.get(p["confidence"], 0)) if classified else members[0]
        result.append({
            "pages": [p["path"] for p in members],
            "doc_type": best["doc_type"],
            "category": best["category"],
            "confidence": best["confidence"],
        })
    return result


def detect_and_split_documents(pdf_path: str, temp_dir: str) -> list:
    with fitz.open(pdf_path) as doc:
        total_pages = len(doc)

    if total_pages <= 1:
        return [_classify_pdf(pdf_path)]

    print(f"📑 Multi-page PDF detected ({total_pages} pages) — checking for multiple documents...")

    pages_dir = os.path.join(temp_dir, "pages")
    page_paths = split_pdf_into_pages(pdf_path, pages_dir)

    page_classifications = []
    for page_path in page_paths:
        classified = _classify_pdf(page_path)
        page_classifications.append(classified)
        print(f"   Page classified as: {classified['doc_type']}")

    groups = group_pages_by_document(page_classifications)

    print(f"📦 Detected {len(groups)} separate document(s) inside this PDF")

    # Everything classified the same (or a single group): keep the original file as is.
    if len(groups) == 1 and len(groups[0]["pages"]) == total_pages:
        return [{
            "path": pdf_path,
            "doc_type": groups[0]["doc_type"],
            "category": groups[0]["category"],
            "confidence": groups[0]["confidence"],
        }]

    final_documents = []

    for idx, group in enumerate(groups):
        merged_path = os.path.join(temp_dir, f"document_{idx + 1}.pdf")
        merge_pages_into_pdf(group["pages"], merged_path)

        final_documents.append({
            "path": merged_path,
            "doc_type": group["doc_type"],
            "category": group["category"],
            "confidence": group["confidence"],
        })

    return final_documents
