import os
import requests
from flask import Flask, request
from openai import OpenAI
from datetime import datetime, timezone

app = Flask(__name__)

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")
WHATSAPP_ACCESS_TOKEN = os.environ.get("WHATSAPP_ACCESS_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

openai_client = OpenAI(api_key=OPENAI_API_KEY)


SYSTEM_INSTRUCTIONS = """
You are the first-line WhatsApp customer service assistant for
Good Deal Properties.

Your job is to politely welcome customers and help with simple
property-related enquiries.

Keep replies short, friendly and natural for WhatsApp.

IMPORTANT CONVERSATION RULES:

1. Remember everything the customer has already told you in this conversation.
2. Do NOT ask the same question again if the customer has already answered it.
3. Have a natural conversation, one or two questions at a time.
4. Do not ask all qualification questions at once.
5. Use information already provided by the customer.

Try to understand these customer details naturally:
- Name
- Own stay or investment
- Preferred location
- Budget
- Property type
- Which property/project they are interested in

Do not make up property information.
Do not promise discounts.
Do not negotiate prices.
Do not give legal, tax or loan advice.

If the customer asks for the latest price, availability,
special discount, negotiation, legal advice, loan approval,
or anything you are unsure about, tell them that a property
consultant will assist them.

You are a first-line assistant, not the salesperson.
Do not try to close the deal yourself.
"""


def supabase_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json"
    }


def get_customer(phone):
    url = f"{SUPABASE_URL}/rest/v1/customers"

    params = {
        "whatsapp_phone": f"eq.{phone}",
        "select": "*",
        "limit": "1"
    }

    response = requests.get(
        url,
        headers=supabase_headers(),
        params=params,
        timeout=15
    )

    response.raise_for_status()

    data = response.json()

    if data:
        return data[0]

    return None


def create_customer(phone):
    url = f"{SUPABASE_URL}/rest/v1/customers"

    payload = {
        "whatsapp_phone": phone,
        "lead_status": "New Lead"
    }

    headers = supabase_headers()
    headers["Prefer"] = "return=representation"

    response = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=15
    )

    response.raise_for_status()

    data = response.json()

    return data[0]


def save_message(customer_id, sender, message, whatsapp_message_id=None):
    url = f"{SUPABASE_URL}/rest/v1/messages"

    payload = {
        "customer_id": customer_id,
        "sender": sender,
        "message": message
    }

    if whatsapp_message_id:
        payload["whatsapp_message_id"] = whatsapp_message_id

    headers = supabase_headers()
    headers["Prefer"] = "return=minimal"

    response = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=15
    )

    response.raise_for_status()
def update_customer(customer_id, profile):
    url = f"{SUPABASE_URL}/rest/v1/customers"

    payload = {}

    for field in [
        "name",
        "intent",
        "location",
        "budget",
        "property_type",
        "interested_property",
        "lead_status"
    ]:
        value = profile.get(field)

        if value is not None and value != "":
            payload[field] = value

    payload["last_message_at"] = datetime.now(timezone.utc).isoformat()

    headers = supabase_headers()
    headers["Prefer"] = "return=minimal"

    response = requests.patch(
        url,
        headers=headers,
        params={"id": f"eq.{customer_id}"},
        json=payload,
        timeout=15
    )

    response.raise_for_status()

def get_conversation_history(customer_id):
    url = f"{SUPABASE_URL}/rest/v1/messages"

    params = {
        "customer_id": f"eq.{customer_id}",
        "select": "sender,message",
        "order": "created_at.asc",
        "limit": "20"
    }

    response = requests.get(
        url,
        headers=supabase_headers(),
        params=params,
        timeout=15
    )

    response.raise_for_status()

    data = response.json()

    history = []

    for item in data:
        if item["sender"] == "customer":
            history.append({
                "role": "user",
                "content": item["message"]
            })

        elif item["sender"] == "ai":
            history.append({
                "role": "assistant",
                "content": item["message"]
            })

    return history


@app.route("/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200

    return "Verification failed", 403


@app.route("/webhook", methods=["POST"])
def webhook():

    data = request.get_json()

    print("Incoming WhatsApp message:", data)

    try:
        value = data["entry"][0]["changes"][0]["value"]

        messages = value.get("messages", [])

        if not messages:
            return "EVENT_RECEIVED", 200

        message = messages[0]

        if message.get("type") != "text":
            return "EVENT_RECEIVED", 200

        customer_phone = message["from"]
        customer_message = message["text"]["body"]
        whatsapp_message_id = message.get("id")

        print("Customer:", customer_phone)
        print("Message:", customer_message)

        # Find or create customer
        customer = get_customer(customer_phone)

        if not customer:
            customer = create_customer(customer_phone)

        customer_id = customer["id"]

        print("Customer ID:", customer_id)

        # Save customer message
        save_message(
            customer_id,
            "customer",
            customer_message,
            whatsapp_message_id
        )

        # Load conversation history from database
        conversation_history = get_conversation_history(customer_id)

        # Ask OpenAI
        response = openai_client.responses.create(
            model="gpt-5.6-luna",
            instructions=SYSTEM_INSTRUCTIONS,
            input=conversation_history
        )

        ai_reply = response.output_text

        print("AI Reply:", ai_reply)

        # Save AI reply
        save_message(
            customer_id,
            "ai",
            ai_reply
        )

        # Send reply through WhatsApp
        url = (
            f"https://graph.facebook.com/v26.0/"
            f"{WHATSAPP_PHONE_NUMBER_ID}/messages"
        )

        headers = {
            "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
            "Content-Type": "application/json"
        }

        payload = {
            "messaging_product": "whatsapp",
            "to": customer_phone,
            "type": "text",
            "text": {
                "body": ai_reply
            }
        }

        result = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=30
        )

        print(
            "WhatsApp API response:",
            result.status_code,
            result.text
        )

    except Exception as e:
        print("ERROR:", str(e))

    return "EVENT_RECEIVED", 200


@app.route("/", methods=["GET"])
def home():
    return "WA AI Assistant Backend is running", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
