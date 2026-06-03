import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from contextlib import asynccontextmanager

from database import init_db, setting
from auth import ensure_admin_exists
from config import STATIC_DIR

from routers.auth_router import router as auth_r
from routers.clients_router import router as clients_r
from routers.documents_router import router as docs_r
from routers.organizer_router import router as org_r
from routers.ai_router import router as ai_r
from routers.settings_router import router as settings_r

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_db()
    ensure_admin_exists()

    from services.scanner import sync_all
    from services.indexer import index_all_pending
    from services import watcher

    print("[KOVA] Syncing client folders...")
    sync_all()
    print("[KOVA] Indexing new documents...")
    n = index_all_pending()
    print(f"[KOVA] Queued {n} documents for indexing.")

    if setting("watcher_enabled") == "1":
        from pathlib import Path as _Path
        def _on_fs_change(folder: _Path):
            if folder.name.startswith("_novo"):
                from services.identifier import refresh_file_count
                refresh_file_count(folder.name)
            else:
                sync_all()
        watcher.start(
            scanner_sync_fn=_on_fs_change,
            indexer_single_fn=lambda path: None,
        )

    yield
    # Shutdown
    watcher.stop()

app = FastAPI(title="Kova Finance", lifespan=lifespan)

STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(auth_r)
app.include_router(clients_r)
app.include_router(docs_r)
app.include_router(org_r)
app.include_router(ai_r)
app.include_router(settings_r)
