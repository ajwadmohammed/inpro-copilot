import os
import sys
from pathlib import Path

os.environ["INPRO_NO_AUTOAPP"] = "1"          # importing the API must not create a database
os.environ["INPRO_NO_DOTENV"] = "1"           # tests never read your real .env (keys, paid flags)
_keep = ("INPRO_NO_AUTOAPP", "INPRO_NO_DOTENV")
for _k in [k for k in os.environ if k.startswith("INPRO_") and k not in _keep]:
    del os.environ[_k]
os.environ["INPRO_AUTH"] = "0"
os.environ["INPRO_INTAKE"] = "0"              # no background inbox watcher during tests                # most tests exercise features; test_auth.py switches sign-in on explicitly
for _k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ.pop(_k, None)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
