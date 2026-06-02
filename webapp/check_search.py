import sys
sys.path.insert(0, r'C:\Users\anton\Desktop\Kova Finance\webapp')
from database import get_db
with get_db() as db:
    total_docs = db.execute('SELECT COUNT(*) FROM documents').fetchone()[0]
    indexed = db.execute('SELECT COUNT(*) FROM documents WHERE indexed_at IS NOT NULL').fetchone()[0]
    fts_rows = db.execute('SELECT COUNT(*) FROM documents_fts').fetchone()[0]
    print(f'Total documents: {total_docs}')
    print(f'Indexed: {indexed}')
    print(f'FTS rows: {fts_rows}')
    # Sample search
    from services.indexer import search
    results = search('contrato', limit=5)
    print(f'Search "contrato" results: {len(results)}')
    results2 = search('CC_Alexandra', limit=5)
    print(f'Search "CC_Alexandra" results: {len(results2)}')
    if results2:
        print(f'  First: {results2[0]}')
