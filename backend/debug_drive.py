from backend.database import SessionLocal
from backend.services.drive_service import get_drive_service, get_main_bgv_folder, get_or_create_folder
import time

db = SessionLocal()
service = get_drive_service('lotusbazaz@gmail.com', db)
main_folder_id = get_main_bgv_folder(service)

print('Step 1: creating candidate main folder')
start = time.time()
candidate_folder_id = get_or_create_folder(service, 'C202-Lkshay-test', main_folder_id)
print('Done in', time.time() - start, 'seconds:', candidate_folder_id)

categories = ['Identity', 'Address', 'Education', 'Employment', 'Financial', 'Professional', 'Compliance', 'Photo_Personal', 'International']

for cat in categories:
    print(f'Creating subfolder: {cat}')
    start = time.time()
    folder_id = get_or_create_folder(service, cat, candidate_folder_id)
    print(f'  Done in {time.time() - start:.2f} seconds:', folder_id)