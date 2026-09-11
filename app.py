import os
import requests
from flask import Flask, request
from openai import OpenAI

app = Flask(__name__)

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")
WHATSAPP_ACCESS_TOKEN = os.environ.get("WHATSAPP_ACCESS_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

openai_client = OpenAI(api_key=OPENAI_API_KEY)


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

        # Only process text messages
        if message.get("type") != "text":
            return "EVENT_RECEIVED", 200

        customer_phone = message["from"]
        customer_message = message["text"]["body"]

        print("Customer:", customer_phone)
        print("Message:", customer_message)

        # Ask OpenAI
        response = openai_client.responses.create(
            model="gpt-5.6-luna",
            instructions="""
You are the first-line WhatsApp customer service assistant for
Good Deal Properties.

Your job is to politely welcome customers and answer simple
property-related enquiries.

Keep replies short, friendly and natural for WhatsApp.

Do not make up property information.
Do not promise discounts.
Do not negotiate prices.
Do not give legal, tax or loan advice.

If you do not know the answer, tell the customer that a property
consultant will assist them.

For a new customer, try to understand:
1. Name
2. Own stay or investment
3. Preferred location
4. Budget
5. Property type

Do not ask all questions at once. Have a natural conversation.
""",
            input=customer_message
        )

        ai_reply = response.output_text

        print("AI Reply:", ai_reply)

        # Send reply through WhatsApp Cloud API
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

        print("WhatsApp API response:", result.status_code, result.text)

    except Exception as e:
        print("ERROR:", str(e))

    return "EVENT_RECEIVED", 200


@app.route("/", methods=["GET"])
def home():
    return "WA AI Assistant Backend is running", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
