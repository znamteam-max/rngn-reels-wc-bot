from __future__ import annotations

import json
from urllib.request import urlopen

URL = "https://project-dcd2y.vercel.app/api/internal/check-egor-four"

with urlopen(URL, timeout=20) as response:
    payload = json.loads(response.read().decode("utf-8"))
if not payload.get("ok"):
    raise SystemExit("live check failed")
print(json.dumps(payload, ensure_ascii=False))
