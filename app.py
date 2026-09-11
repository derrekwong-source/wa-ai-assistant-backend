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

# Temporary conversation memory
# Key = customer WhatsApp number
conversations = {}


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

For example:
If the customer already told you they are interested in Nadayu 28,
do not ask which property they are interested in again.

If the customer already gave their budget,
do not ask their budget again.

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

        # Create conversation memory for new customer
        if customer_phone not in conversations:
            conversations[customer_phone] = []

        # Add customer's message
        conversations[customer_phone].append({
            "role": "user",
            "content": customer_message
        })

        # Keep the latest 20 messages
        conversations[customer_phone] = conversations[customer_phone][-20:]

        # Ask OpenAI with conversation history
        response = openai_client.responses.create(
            model="gpt-5.6-luna",
            instructions=SYSTEM_INSTRUCTIONS,
            input=conversations[customer_phone]
        )

        ai_reply = response.output_text

        print("AI Reply:", ai_reply)

        # Save AI reply into conversation memory
        conversations[customer_phone].append({
            "role": "assistant",
            "content": ai_reply
        })

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
