import httpx

url = "https://graph.facebook.com/v21.0/1270626386131979/messages"
# PASTE YOUR NEW ACCESS TOKEN HERE:
token = "EAATcWl5hBDABSSda1Wm8alvO4feLjDgbRI9MKTzsLuoytPZCepi8JagJ5WlVDzLm5Qce9oHZC0pvwpbV2JtOJMzm7Wy7LF7CY4vB5d55Jnvstlo7J2T5yeSciRljCTfZBq3gS0B4z55s9xE7BPwpLQDnczHQVF3F5taI6ZCedaTtF1Uh9DpiT7GoTdDCCZCxFTHporWq2LdAmBZA0Qf2ICTWuTG4l6M04ewrnHEiW4IgXvpu26bZAZAzZCZAclXHlgsGz8NE0NRgjZBXoUu8R6ByCFkVLkZD" 

payload = {
    "messaging_product": "whatsapp",
    "to": "8801400530058",  # Your Bangladesh number
    "type": "text",
    "text": {"body": "BOOKABL send test from Python!"}
}

print("Sending test message...")
r = httpx.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload)
print(f"Status: {r.status_code}")
print(f"Response: {r.text}")