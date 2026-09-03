import httpx
import os

# Load token from .env
token = ""
for line in open(".env", encoding="utf-8"):
    line = line.strip()
    if line.startswith("WA_ACCESS_TOKEN="):
        token = line.split("=", 1)[1].strip()
        break

print(f"Using token: {token[:20]}...{token[-10:]}")

url = "https://graph.facebook.com/v21.0/1270626386131979/messages"
headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json"
}
payload = {
    "messaging_product": "whatsapp",
    "to": "8801400530058",
    "type": "text",
    "text": {"body": "Token check - this should arrive on your WhatsApp!"}
}

resp = httpx.post(url, headers=headers, json=payload)
print(f"Status: {resp.status_code}")
print(f"Response: {resp.text}")