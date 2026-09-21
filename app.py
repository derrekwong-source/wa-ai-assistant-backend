import os
import requests
import json
import re
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

Keep replies short, friendly and natural for WhatsApp.

IMPORTANT:

1. Always prioritize the customer's LATEST message.
2. Do not answer an older question if the latest message has a new request.
3. Remember information already provided by the customer.
4. Do not ask for information that is already known.
5. Do not invent property information.
6. Do not promise discounts.
7. Do not negotiate prices.
8. Do not guarantee availability.
9. You are a first-line assistant, not the salesperson.

Customer information:
- Name
- Own Stay or Investment
- Location
- Budget
- Property Type
- Interested Property

If the latest customer message changes the budget, location,
or property type, always use the NEW information.

If a suitable listing is provided by the database, use that
listing as the source of truth.
"""


# =========================================================
# SUPABASE
# =========================================================

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

    return data[0] if data else None


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

    return response.json()[0]


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

    payload["last_message_at"] = datetime.now(
        timezone.utc
    ).isoformat()

    headers = supabase_headers()
    headers["Prefer"] = "return=minimal"

    response = requests.patch(
        url,
        headers=headers,
        params={
            "id": f"eq.{customer_id}"
        },
        json=payload,
        timeout=15
    )

    response.raise_for_status()


def save_message(
    customer_id,
    sender,
    message,
    whatsapp_message_id=None
):
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


# =========================================================
# BUDGET
# =========================================================

def parse_budget(value):

    if not value:
        return None

    text = str(value).lower()
    text = text.replace(",", "")
    text = text.replace(" ", "")

    match = re.search(
        r"rm?(\d+(?:\.\d+)?)m(?:illion)?",
        text
    )

    if match:
        return float(match.group(1)) * 1_000_000

    match = re.search(
        r"rm?(\d+(?:\.\d+)?)",
        text
    )

    if match:
        number = float(match.group(1))

        if number < 10000:
            return number * 1_000_000

        return number

    return None


def parse_price(value):

    if not value:
        return None

    text = str(value).lower()
    text = text.replace(",", "")
    text = text.replace(" ", "")

    match = re.search(
        r"rm?(\d+(?:\.\d+)?)(m|million)?",
        text
    )

    if not match:
        return None

    number = float(match.group(1))

    if match.group(2):
        number *= 1_000_000

    return number


# =========================================================
# LISTING SEARCH
# =========================================================

def search_listings(
    location=None,
    property_type=None,
    budget=None
):

    url = f"{SUPABASE_URL}/rest/v1/listings"

    params = {
        "select": "*",
        "order": "created_at.desc",
        "limit": "50"
    }

    if location:
        params["location"] = f"ilike.*{location}*"

    if property_type:
        params["property_type"] = f"ilike.*{property_type}*"

    response = requests.get(
        url,
        headers=supabase_headers(),
        params=params,
        timeout=15
    )

    response.raise_for_status()

    listings = response.json()

    budget_value = parse_budget(budget)

    suitable = []

    for listing in listings:

        status = str(
            listing.get("property_status") or ""
        ).lower()

        unavailable = [
            "sold",
            "rented",
            "taken",
            "unavailable",
            "closed"
        ]

        if any(
            word in status
            for word in unavailable
        ):
            continue

        asking_price = parse_price(
            listing.get("asking_price")
        )

        if budget_value is not None:
            if asking_price is not None:
                if asking_price > budget_value:
                    continue

        suitable.append(listing)

    return suitable


def format_listings(listings):

    if not listings:
        return "NO MATCHING LISTINGS FOUND."

    result = []

    for listing in listings:

        result.append({
            "listing_id":
                listing.get("listing_id"),

            "property_type":
                listing.get("property_type"),

            "location":
                listing.get("location"),

            "land_area":
                listing.get("land_area"),

            "built_up":
                listing.get("built_up"),

            "asking_price":
                listing.get("asking_price"),

            "tenure":
                listing.get("tenure"),

            "property_status":
                listing.get("property_status"),

            "suitable_for":
                listing.get("suitable_for"),

            "description":
                listing.get("description")
        })

    return json.dumps(
        result,
        ensure_ascii=False,
        indent=2
    )


# =========================================================
# WEBHOOK VERIFY
# =========================================================

@app.route("/webhook", methods=["GET"])
def verify():

    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if (
        mode == "subscribe"
        and token == VERIFY_TOKEN
    ):
        return challenge, 200

    return "Verification failed", 403


# =========================================================
# WHATSAPP WEBHOOK
# =========================================================

@app.route("/webhook", methods=["POST"])
def webhook():

    data = request.get_json()

    print(
        "Incoming WhatsApp message:",
        data
    )

    try:

        value = (
            data["entry"][0]
            ["changes"][0]
            ["value"]
        )

        messages = value.get(
            "messages",
            []
        )

        if not messages:
            return "EVENT_RECEIVED", 200

        message = messages[0]

        if message.get("type") != "text":
            return "EVENT_RECEIVED", 200

        customer_phone = message["from"]

        customer_message = (
            message["text"]["body"]
        )

        whatsapp_message_id = message.get(
            "id"
        )

        print(
            "Customer:",
            customer_phone
        )

        print(
            "Message:",
            customer_message
        )

        # -------------------------------------------------
        # CUSTOMER
        # -------------------------------------------------

        customer = get_customer(
            customer_phone
        )

        if not customer:
            customer = create_customer(
                customer_phone
            )

        customer_id = customer["id"]

        print(
            "Customer ID:",
            customer_id
        )

        # -------------------------------------------------
        # SAVE CUSTOMER MESSAGE
        # -------------------------------------------------

        save_message(
            customer_id,
            "customer",
            customer_message,
            whatsapp_message_id
        )

        # -------------------------------------------------
        # HISTORY
        # -------------------------------------------------

        conversation_history = (
            get_conversation_history(
                customer_id
            )
        )

        # -------------------------------------------------
        # EXTRACT CUSTOMER PROFILE
        # -------------------------------------------------

        profile_response = (
            openai_client.responses.create(

                model="gpt-5.6-luna",

                instructions=SYSTEM_INSTRUCTIONS
                + """

Extract the customer's profile from the conversation.

IMPORTANT:
The latest customer message has priority when information
has changed.

Do not guess.

Return null when information is unknown.

For budget keep the customer's wording.

For intent use:
- Own Stay
- Investment

For lead_status use:
New Lead

Do not create or invent a property description for
interested_property.
Only record a specific property if the customer explicitly
identified one.
""",

                input=conversation_history,

                text={
                    "format": {
                        "type": "json_schema",

                        "name": "customer_profile",

                        "schema": {

                            "type": "object",

                            "properties": {

                                "name": {
                                    "type": [
                                        "string",
                                        "null"
                                    ]
                                },

                                "intent": {
                                    "type": [
                                        "string",
                                        "null"
                                    ]
                                },

                                "location": {
                                    "type": [
                                        "string",
                                        "null"
                                    ]
                                },

                                "budget": {
                                    "type": [
                                        "string",
                                        "null"
                                    ]
                                },

                                "property_type": {
                                    "type": [
                                        "string",
                                        "null"
                                    ]
                                },

                                "interested_property": {
                                    "type": [
                                        "string",
                                        "null"
                                    ]
                                },

                                "lead_status": {
                                    "type": [
                                        "string",
                                        "null"
                                    ]
                                }
                            },

                            "required": [
                                "name",
                                "intent",
                                "location",
                                "budget",
                                "property_type",
                                "interested_property",
                                "lead_status"
                            ],

                            "additionalProperties": False
                        },

                        "strict": True
                    }
                }
            )
        )

        customer_profile = json.loads(
            profile_response.output_text
        )

        print(
            "Customer Profile:",
            customer_profile
        )

        update_customer(
            customer_id,
            customer_profile
        )

        # -------------------------------------------------
        # SEARCH LISTINGS
        # -------------------------------------------------

        location = customer_profile.get(
            "location"
        )

        property_type = customer_profile.get(
            "property_type"
        )

        budget = customer_profile.get(
            "budget"
        )

        matching_listings = []

        if (
            location
            and property_type
            and budget
        ):

            matching_listings = search_listings(
                location=location,
                property_type=property_type,
                budget=budget
            )

        print(
            "Matching Listings:",
            matching_listings
        )

        listing_context = format_listings(
            matching_listings
        )

        # -------------------------------------------------
        # FINAL RESPONSE
        # IMPORTANT:
        # LATEST MESSAGE ONLY HAS PRIORITY
        # -------------------------------------------------

        final_prompt = """
Write the customer's WhatsApp reply.

CRITICAL RULE:

The customer's LATEST MESSAGE is the highest priority.

Do NOT answer an older question simply because it appeared
earlier in the conversation.

LATEST CUSTOMER MESSAGE:
""" + customer_message + """

CUSTOMER PROFILE:
""" + json.dumps(
            customer_profile,
            ensure_ascii=False,
            indent=2
        ) + """

MATCHING LISTINGS FROM DATABASE:
""" + listing_context + """

RULES:

1. If the latest message is a new budget/location/property
   requirement and a matching listing exists:
   recommend the matching listing immediately.

2. If the latest message asks for price:
   answer the price from the listing.

3. If the latest message asks for land size:
   answer the land size from the listing.

4. If the latest message asks about availability:
   say the listing is currently marked as available in the
   database, but a property consultant should confirm the
   latest status.

5. If the latest message asks about negotiation:
   do not confirm that it is negotiable.
   Say a property consultant can advise on the seller's terms.

6. If the latest message asks for a viewing:
   say a property consultant can help arrange it and ask
   for a convenient date and time.

7. If matching listings exist and the latest message is a
   general property enquiry:
   present the suitable listing.

8. If no matching listing exists:
   clearly say no suitable listing was found based on the
   customer's latest requirement.

9. Never recommend a listing above the customer's latest budget.

10. Never invent listing information.

11. Do not repeat old questions unnecessarily.

Keep the reply short and natural for WhatsApp.
"""

        final_response = (
            openai_client.responses.create(
                model="gpt-5.6-luna",

                instructions=final_prompt,

                # IMPORTANT:
                # Do not send the entire old conversation here.
                # Only give the latest customer message plus
                # current profile and current listings.
                input=[
                    {
                        "role": "user",
                        "content": customer_message
                    }
                ]
            )
        )

        ai_reply = (
            final_response.output_text.strip()
        )

        print(
            "AI Reply:",
            ai_reply
        )

        # -------------------------------------------------
        # SAVE AI MESSAGE
        # -------------------------------------------------

        save_message(
            customer_id,
            "ai",
            ai_reply
        )

        # -------------------------------------------------
        # SEND WHATSAPP
        # -------------------------------------------------

        url = (
            "https://graph.facebook.com/v26.0/"
            f"{WHATSAPP_PHONE_NUMBER_ID}/messages"
        )

        headers = {
            "Authorization":
                f"Bearer {WHATSAPP_ACCESS_TOKEN}",

            "Content-Type":
                "application/json"
        }

        payload = {

            "messaging_product":
                "whatsapp",

            "to":
                customer_phone,

            "type":
                "text",

            "text": {
                "body":
                    ai_reply
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

        print(
            "ERROR:",
            str(e)
        )

    return "EVENT_RECEIVED", 200


# =========================================================
# HOME
# =========================================================

@app.route("/", methods=["GET"])
def home():

    return (
        "WA AI Assistant Backend is running",
        200
    )


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=10000
    )
