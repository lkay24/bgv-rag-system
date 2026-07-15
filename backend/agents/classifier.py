import os
import fitz
import json
import pytesseract
from pdf2image import convert_from_path
from PIL import ImageEnhance, ImageFilter, ImageOps
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

client = Groq(api_key=os.getenv("GROQ_API_KEY"))


def extract_text_from_pdf(pdf_path: str) -> str:
    try:
        doc = fitz.open(pdf_path)

        full_text = ""
        for page_number in range(len(doc)):
            page = doc[page_number]
            full_text += page.get_text()

        doc.close()

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


def extract_text_using_ocr(pdf_path: str) -> str:
    try:
        images = convert_from_path(pdf_path, dpi=300)

        full_text = ""
        for image in images:
            image = preprocess_image_for_ocr(image)

            best_text = ""
            best_length = 0

            for angle in [0, 90, 180, 270]:
                rotated = image.rotate(angle, expand=True) if angle != 0 else image

                try:
                    page_text = pytesseract.image_to_string(rotated, lang='eng+hin')
                except Exception:
                    page_text = pytesseract.image_to_string(rotated)

                if len(page_text.strip()) > best_length:
                    best_length = len(page_text.strip())
                    best_text = page_text

            full_text += best_text

        print(f"✅ OCR extracted {len(full_text)} characters from {pdf_path}")
        return full_text

    except Exception as e:
        print(f"❌ OCR failed for {pdf_path}: {e}")
        return ""

        print(f"✅ OCR extracted {len(full_text)} characters from {pdf_path}")
        return full_text

    except Exception as e:
        print(f"❌ OCR failed for {pdf_path}: {e}")
        return ""


def classify_document(text: str) -> dict:
    if not text or len(text.strip()) < 10:
        return {
            "category": "unknown",
            "doc_type": "unknown",
            "confidence": "low",
            "reasoning": "Could not extract text from PDF"
        }

    print(f"\n{'='*40}")
    print(f"📝 EXTRACTED TEXT BEING SENT TO AI:")
    print(text[:500])
    print(f"{'='*40}\n")

    prompt = f"""
You are a document classification expert for Indian HR background verification.

I will give you text extracted from a document.
Your job is to identify what TYPE of document it is.

DOCUMENT CATEGORIES AND TYPES:
- identity: aadhar_card, pan_card, passport, driving_license, voter_id
- address: electricity_bill, water_bill, gas_bill, bank_statement, rental_agreement
- education: 10th_marksheet, 10th_certificate, 12th_marksheet, 12th_certificate, degree_certificate, provisional_certificate, pg_degree, semester_marksheet
- employment: offer_letter, experience_letter, relieving_letter, appointment_letter, payslip, form_16, joining_letter
- financial: itr, salary_slip, cancelled_cheque
- photo_personal: photograph, signature, resume
- professional: aws_certificate, azure_certificate, pmp_certificate, skill_certificate
- compliance: nda, background_consent, police_verification, drug_test
- international: visa, work_permit, immigration_document

IMPORTANT TIE-BREAK RULE:
Some documents legitimately serve more than one purpose in real life.
For example, an Aadhaar Card can prove both identity AND address, and a
PAN Card can prove both identity AND financial details. Even so, you must
always assign these documents to ONE category only, using this fixed rule:

- aadhar_card → always category "identity", never "address"
- pan_card → always category "identity", never "financial"
- passport → always category "identity", never "address"

Never split, duplicate, or second-guess this — identity always wins for
these three document types, regardless of what else the document shows.

INSTRUCTIONS:
1. Read the document text carefully
2. Identify the category and specific type
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
        response = client.chat.completions.create(
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
            temperature=0
        )

        ai_response = response.choices[0].message.content.strip()

        if "```json" in ai_response:
            ai_response = ai_response.split("```json")[1].split("```")[0].strip()
        elif "```" in ai_response:
            ai_response = ai_response.split("```")[1].split("```")[0].strip()

        result = json.loads(ai_response)

        print(f"🤖 AI classified: {result['category']} → {result['doc_type']} ({result['confidence']} confidence)")

        return result

    except Exception as e:
        print(f"❌ AI classification error: {e}")
        return {
            "category": "unknown",
            "doc_type": "unknown",
            "confidence": "low",
            "reasoning": f"Classification failed: {str(e)}"
        }


def process_pdf(pdf_path: str) -> dict:
    print(f"\n📄 Processing: {pdf_path}")

    text = extract_text_from_pdf(pdf_path)
    classification = classify_document(text)

    classification['pdf_path'] = pdf_path
    classification['filename'] = os.path.basename(pdf_path)

    return classification


def get_suggested_filename(doc_type: str) -> str:

    filename_map = {
        "aadhar_card": "aadhar.pdf",
        "pan_card": "pan.pdf",
        "passport": "passport.pdf",
        "driving_license": "driving_license.pdf",
        "voter_id": "voter_id.pdf",
        "10th_marksheet": "10th_marksheet.pdf",
        "10th_certificate": "10th_certificate.pdf",
        "12th_marksheet": "12th_marksheet.pdf",
        "12th_certificate": "12th_certificate.pdf",
        "degree_certificate": "degree_certificate.pdf",
        "pg_degree": "pg_degree.pdf",
        "offer_letter": "offer_letter.pdf",
        "experience_letter": "experience_letter.pdf",
        "relieving_letter": "relieving_letter.pdf",
        "payslip": "payslip.pdf",
        "form_16": "form_16.pdf",
        "resume": "resume.pdf",
        "nda": "nda.pdf",
        "police_verification": "police_verification.pdf",
    }

    return filename_map.get(doc_type, f"{doc_type}.pdf")


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


def detect_and_split_documents(pdf_path: str, temp_dir: str) -> list:
    doc = fitz.open(pdf_path)
    total_pages = len(doc)
    doc.close()

    if total_pages <= 1:
        text = extract_text_from_pdf(pdf_path)
        classification = classify_document(text)
        return [{
            "path": pdf_path,
            "doc_type": classification["doc_type"],
            "category": classification["category"],
            "confidence": classification["confidence"]
        }]

    print(f"📑 Multi-page PDF detected ({total_pages} pages) — checking for multiple documents...")

    pages_dir = os.path.join(temp_dir, "pages")
    page_paths = split_pdf_into_pages(pdf_path, pages_dir)

    page_classifications = []
    for page_path in page_paths:
        text = extract_text_from_pdf(page_path)
        classification = classify_document(text)
        page_classifications.append({
            "path": page_path,
            "doc_type": classification["doc_type"],
            "category": classification["category"],
            "confidence": classification["confidence"]
        })
        print(f"   Page classified as: {classification['doc_type']}")

    groups = []
    current_group = [page_classifications[0]]

    for i in range(1, len(page_classifications)):
        current = page_classifications[i]
        previous = page_classifications[i - 1]

        if current["doc_type"] == previous["doc_type"]:
            current_group.append(current)
        else:
            groups.append(current_group)
            current_group = [current]

    groups.append(current_group)

    print(f"📦 Detected {len(groups)} separate document(s) inside this PDF")

    final_documents = []

    for idx, group in enumerate(groups):
        group_paths = [p["path"] for p in group]
        merged_path = os.path.join(temp_dir, f"document_{idx + 1}.pdf")
        merge_pages_into_pdf(group_paths, merged_path)

        final_documents.append({
            "path": merged_path,
            "doc_type": group[0]["doc_type"],
            "category": group[0]["category"],
            "confidence": group[0]["confidence"]
        })

    return final_documents