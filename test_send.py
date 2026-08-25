import httpx

url = "https://graph.facebook.com/v21.0/1270626386131979/messages"
# PASTE YOUR NEW ACCESS TOKEN HERE:
token = "EAAWgx7IPT5IBSVRicXg7pZCcHgIH2OC8ZC3mUXWskeJBZAZBlWmRmeF0Sd56D1JtImQZAmGW4XBJEpT5IhWcnm5V8bNXyQFZBchhWYe90BYsDlZBinfxnYnsChlVnZA6R2tG2Uc36SCiGUf7if89lxQSqSZCJSpQrZBbNCWZCF2W43YWpROojoQvAoDbuzdgEchlldPKwRZACetFCxZAG1YZAQZCinIDXWeZAnnJeWLu7AKZBXEDZBLPYJhXn5vNDgHH4ZApW7e2vhCv98d22zi9vFyIHEzIFbnrwZDZD"

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