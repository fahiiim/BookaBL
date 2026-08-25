import hashlib, hmac, json, time, uuid
import httpx

env = {}
for line in open(".env", encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()

API = "http://127.0.0.1:8001/webhooks/whatsapp"
SECRET = env["WA_APP_SECRET"].encode()
SB = env["SUPABASE_URL"]; SB_KEY = env["SUPABASE_SERVICE_ROLE_KEY"]
PHONE_ID = env["WA_PHONE_NUMBER_ID"]; FROM = "8801400530058"
H = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}

def send(text):
    body = json.dumps({"object": "whatsapp_business_account", "entry": [{"id": "10131956380006734",
        "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "15552001547", "phone_number_id": PHONE_ID},
            "contacts": [{"profile": {"name": "Fahim Test"}, "wa_id": FROM}],
            "messages": [{"from": FROM, "id": f"wamid.sim.{uuid.uuid4().hex[:16]}",
                           "timestamp": str(int(time.time())), "type": "text", "text": {"body": text}}],
        }}]}]}).encode()
    sig = "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()
    print(f"sent {text!r} ->", httpx.post(API, content=body, timeout=30,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig}).status_code)

def state():
    r = httpx.get(f"{SB}/rest/v1/conversation_states",
                  params={"select": "state,slot", "order": "updated_at.desc", "limit": "1"}, headers=H)
    return (r.json() or [{}])[0]

def wait_for(key, timeout=30):
    for _ in range(timeout):
        time.sleep(1)
        slots = state().get("slot") or {}
        if slots.get(key):
            return slots
    raise SystemExit(f"timeout waiting for {key}")

send("book appointment"); wait_for("offered_service_ids")
send("Cleaning");         slots = wait_for("offered_slots")
print("offered slots:", slots)
send("slot:" + slots["offered_slots"][0]); time.sleep(2)   # ← fixed line
send("Discovery Health"); time.sleep(2)
send("1234567");          time.sleep(2)
send("01");               time.sleep(6)
print("DONE -> check Telegram + Supabase")