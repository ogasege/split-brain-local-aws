import sys
from pathlib import Path

# Lambda code lives in app/ and is imported as the `ingest` package, exactly
# as the Lambda runtime sees it (handler = ingest.handler.handler).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
