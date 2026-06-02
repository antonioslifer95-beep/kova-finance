import sys
sys.path.insert(0, r'C:\Users\anton\Desktop\Kova Finance\webapp')
from database import get_db
with get_db() as db:
    rows = db.execute('SELECT id, status, started_at, client_scope FROM organizer_runs ORDER BY id DESC LIMIT 10').fetchall()
    for row in rows:
        print(f'  Run {row["id"]}: status={row["status"]} started={row["started_at"]} scope={row["client_scope"]}')
