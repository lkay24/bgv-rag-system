"""Single source of truth for document categories and types.

The classifier prompt, the settings page vocabulary, required-document validation
and the Drive folder layout are all derived from this module so they cannot drift.
"""

# Order matters: when a type appears under several categories, the first one wins.
# That gives the fixed tie-break the classifier relies on (aadhar_card/passport/pan_card
# are identity, bank_statement is address, form_16 is employment).
DOCUMENT_TYPES = {
    "identity": ["aadhar_card", "pan_card", "passport", "driving_license", "voter_id"],
    "address": [
        "aadhar_card", "passport", "electricity_bill", "water_bill", "gas_bill",
        "bank_statement", "rental_agreement", "telephone_bill",
    ],
    "education": [
        "10th_marksheet", "10th_certificate", "12th_marksheet", "12th_certificate",
        "degree_certificate", "provisional_certificate", "semester_marksheet",
        "consolidated_marksheet", "college_id_card", "pg_degree",
    ],
    "employment": [
        "offer_letter", "appointment_letter", "experience_letter", "relieving_letter",
        "promotion_letter", "salary_revision_letter", "payslip", "form_16",
        "employee_id_card", "joining_letter",
    ],
    "financial": ["pan_card", "form_16", "itr", "salary_slip", "bank_statement", "cancelled_cheque"],
    "photo_personal": ["photograph", "signature", "resume", "biodata"],
    "professional": [
        "aws_certificate", "azure_certificate", "pmp_certificate",
        "google_cloud_certificate", "skill_certificate",
    ],
    "compliance": ["nda", "non_compete_agreement", "background_consent", "police_verification", "drug_test"],
    "international": ["visa", "work_permit", "immigration_document", "overseas_certificate"],
    "criminal": ["police_verification_certificate", "criminal_background_check"],
}

CATEGORIES = list(DOCUMENT_TYPES)

PRIMARY_CATEGORY = {}
for _category, _types in DOCUMENT_TYPES.items():
    for _t in _types:
        PRIMARY_CATEGORY.setdefault(_t, _category)

ALL_DOC_TYPES = set(PRIMARY_CATEGORY)


def normalize_doc_type(value: str) -> str:
    return "_".join(str(value).strip().lower().split())


def prompt_listing() -> str:
    """Category -> primary types, formatted for the classification prompt."""
    lines = []
    for category in CATEGORIES:
        types = [t for t in DOCUMENT_TYPES[category] if PRIMARY_CATEGORY[t] == category]
        if types:
            lines.append(f"- {category}: {', '.join(types)}")
    return "\n".join(lines)
