"""Start the Kova Finance web app."""
import sys, subprocess
from pathlib import Path

if __name__ == "__main__":
    webapp = Path(__file__).parent
    sys.path.insert(0, str(webapp))
    subprocess.run([
        sys.executable, "-m", "uvicorn",
        "main:app",
        "--host", "0.0.0.0",
        "--port", "8080",
        "--reload",
        "--reload-dir", str(webapp),
    ], cwd=str(webapp))
