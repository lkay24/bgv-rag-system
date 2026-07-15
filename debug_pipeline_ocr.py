from backend.database import SessionLocal
from backend.services.gmail_service import get_gmail_service, get_candidate_documents
from backend.agents.classifier import extract_text_from_pdf, classify_document

db = SessionLocal()
service = get_gmail_service('lotusbazaz@gmail.com', db)
pdfs = get_candidate_documents(service, '12alkshaybazaz@gmail.com', 'C202_debug2')

for pdf_path in pdfs:
    print(f'\n=== {pdf_path} ===')
    text = extract_text_from_pdf(pdf_path)
    print('Extracted length:', len(text))
    print('First 200 chars:', repr(text[:200]))
    
    result = classify_document(text)
    print('Classification:', result)
