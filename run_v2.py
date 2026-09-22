"""V2 entry point; also implements the Video Worker subprocess protocol."""
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / '.env', override=False)

sys.path.insert(0, str(Path(__file__).resolve().parent / "services" / "video-worker"))
from v2.pipeline import main

if __name__ == "__main__":
    raise SystemExit(main())
