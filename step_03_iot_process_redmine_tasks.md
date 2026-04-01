anda perlu pelajari /mnt/d/Github/celerates/digital-bast/v1-prod/src/steps/step_03_process_redmine_tasks.py
dimana itu kan source nya dari database,
nah untuk step_03_iot ini berbeda.
jadi source nya adalah dari gsheet dengan field:
Date (2D)
Start Time (2E)
Close Time (2P)
Response Time (2F)
First Responder (2H)
Issue Type (2K)
Issue Description (2M)

LINK: https://docs.google.com/spreadsheets/d/1bzAndOjRR-9GOrB8a2_FD5ayE5uPLLrg7gK4bKcmKbo/edit?usp=sharing

dimana itu perlu di sync ke nocodb dengan table Tasklist IoT Operations, dengan mapping nya:
Tasklist IoT Operations <-> gsheet

Task List = Issue Description
Date = Date
Kategori = Issue Type
Requestor = "User"
Status = "Closed"
Unique Key = Date_Employee Id 
Start Time = Start Time
Response Time = Response Time
Close Time = Close Time
PIC Selection = First Responder

aku pengen pake method yang existing dimana ada insert/upsertnya gitu lalu generate unique key nya juga sama jadi ngga terlalu effort atau malah ngebuat sistem sendiri, tetep seperti reuse gitu loh jadi nya dan ngga over engineer

soalnya udah ada creds akses ke gsheet nya juga library nya juga udah ada jadi harusnya ngga banyak changes nya 
jadi buatlah menjadi step_03_iot_redmine_tasks