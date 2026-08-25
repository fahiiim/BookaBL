import httpx
from app.core.config import get_settings

WABA_ID = "1013195638006734"
GRAPH_API_VERSION = "v23.0"

settings = get_settings()
if settings.wa_access_token is None:
    raise SystemExit("WA_ACCESS_TOKEN is not configured in .env")

url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{WABA_ID}/subscribed_apps"
response = httpx.get(
    url,
    headers={
        "Authorization": f"Bearer {settings.wa_access_token.get_secret_value()}"
    },
    timeout=20,
)

print(response.status_code)
print(response.text)
