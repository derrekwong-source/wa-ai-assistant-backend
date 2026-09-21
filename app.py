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


# =========================================================
# SYSTEM INSTRUCTIONS
# =========================================================

SYSTEM_INSTRUCTIONS = """
You are the first-line WhatsApp customer service assistant for
Good Deal Properties.

Your job is to politely welcome customers and help with simple
property-related enquiries.

Keep replies short, friendly and natural for WhatsApp.

IMPORTANT CONVERSATION RULES:

1. Remember everything the customer has already told you.
2. Do NOT ask the same question again if the customer already answered it.
3. Ask only one or two useful questions at a time.
4. Use the customer's latest information when something changes.
5. If the customer changes their budget, location, property type,
   or other requirement, always use the latest information.
6. Do not ignore an updated budget.

CUSTOMER INFORMATION TO UNDERSTAND NATURALLY:

- Name
- Own stay or investment
- Preferred location
- Budget
- Property type
- Interested property/project

LISTING RULES:

If the system provides matching listings from the database,
you MUST use those listings as the source of truth.

Do NOT invent:
- asking price
- land area
- built-up
- tenure
- location
- property status
- property features

If a matching listing is provided, you may present its information
to the customer.

If no matching listing is provided, do not invent a property.

IMPORTANT:

When the customer already has:
- location
- property type
- budget

the system should search the listing database first.

If matching listings are found:
- mention the suitable listing
- provide the available listing details
- ask whether the customer wants more details or a viewing

Do NOT keep asking qualification questions first when a suitable
listing is already available.

If the customer asks about:
- latest availability
- negotiation
- discount
- viewing
- legal matters
- loan
- anything uncertain

do not make promises or decisions.

Tell the customer that a property consultant can confirm or assist.

You are a first-line assistant, not the salesperson.
Do not try to close the deal yourself.
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


# =========================================================
# CUSTOMER FUNCTIONS
# =========================================================

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


def update_customer(customer_id, profile):
    url = f"{SUPABASE_URL}/rest/v1/customers"

    payload = {}

    fields = [
        "name",
        "intent",
        "location",
        "budget",
        "property_type",
        "interested_property",
        "lead_status"
    ]

    for field in fields:

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


# =========================================================
# MESSAGE FUNCTIONS
# =========================================================

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
# BUDGET PARSER
# =========================================================

def parse_budget(value):

    if not value:
        return None

    text = str(value).lower()
    text = text.replace(",", "")
    text = text.replace(" ", "")

    # RM3m / RM3million
    match = re.search(
        r"rm?(\d+(?:\.\d+)?)m(?:illion)?",
        text
    )

    if match:
        return float(match.group(1)) * 1_000_000

    # RM3,000,000 / RM3000000
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


# =========================================================
# PRICE PARSER
# =========================================================

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

        # -------------------------------------------------
        # PROPERTY STATUS
        # -------------------------------------------------

        status = str(
            listing.get("property_status") or ""
        ).lower()

        if status:

            unavailable_words = [
                "sold",
                "rented",
                "taken",
                "unavailable",
                "closed"
            ]

            if any(
                word in status
                for word in unavailable_words
            ):
                continue

        # -------------------------------------------------
        # PRICE CHECK
        # -------------------------------------------------

        asking_price = parse_price(
            listing.get("asking_price")
        )

        if budget_value is not None:

            if asking_price is not None:

                # Customer budget must be able to cover
                # the asking price.
                if asking_price > budget_value:
                    continue

        suitable.append(listing)

    return suitable


# =========================================================
# FORMAT LISTINGS FOR AI
# =========================================================

def format_listings(listings):

    if not listings:
        return "NO MATCHING LISTINGS FOUND."

    output = []

    for listing in listings:

        item = {
            "listing_id": listing.get("listing_id"),
            "property_type": listing.get("property_type"),
            "location": listing.get("location"),
            "land_area": listing.get("land_area"),
            "built_up": listing.get("built_up"),
            "asking_price": listing.get("asking_price"),
            "tenure": listing.get("tenure"),
            "property_status": listing.get("property_status"),
            "suitable_for": listing.get("suitable_for"),
            "description": listing.get("description")
        }

        output.append(item)

    return json.dumps(
        output,
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
        # GET CONVERSATION
        # -------------------------------------------------

        conversation_history = (
            get_conversation_history(
                customer_id
            )
        )

        # -------------------------------------------------
        # FIRST AI PASS
        #
        # Extract latest customer profile
        # -------------------------------------------------

        profile_response = (
            openai_client.responses.create(

                model="gpt-5.6-luna",

                instructions=SYSTEM_INSTRUCTIONS
                + """

Extract the customer's profile based ONLY
on information already provided in the conversation.

Do not guess.

If a field is unknown, return null.

For budget, keep the customer's wording.

Examples:
- RM3 million
- RM2m
- RM2.5 million

For intent:
- Own Stay
- Investment

If not known, return null.

For lead_status:
use New Lead unless the conversation clearly
indicates a more advanced lead.
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

        # -------------------------------------------------
        # UPDATE CUSTOMER DATABASE
        # -------------------------------------------------

        update_customer(
            customer_id,
            customer_profile
        )

        # -------------------------------------------------
        # LISTING SEARCH
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

        # Search only when enough information exists.
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
        # FINAL AI RESPONSE
        # -------------------------------------------------

        final_instructions = SYSTEM_INSTRUCTIONS + """

You must now write the actual WhatsApp reply.

CUSTOMER PROFILE:

""" + json.dumps(
            customer_profile,
            ensure_ascii=False,
            indent=2
        ) + """

LISTINGS FOUND FROM DATABASE:

""" + listing_context + """

VERY IMPORTANT:

If LISTINGS FOUND contains one or more listings:

1. Prioritize the matching listing.
2. Do not ask unnecessary qualification questions first.
3. Present the listing naturally.
4. Only use the information provided in the listing.
5. You may mention:
   - asking price
   - land area
   - built-up
   - tenure
   - property status
   - suitable_for
   - description
6. Do not invent missing information.
7. Ask if the customer wants more details or a viewing.

If LISTINGS FOUND says:

NO MATCHING LISTINGS FOUND.

Then clearly tell the customer that there is currently
no suitable listing found based on the latest requirement.

If the customer changed their budget, always use
the NEW budget.

Example:

Previous budget: RM3 million
New budget: RM2.5 million

Do NOT recommend a RM3 million property when the
customer's latest budget is RM2.5 million.

If the customer changes back to RM3 million,
the RM3 million listing can become suitable again.

IMPORTANT:
If the customer asks about availability, do not guarantee
that the property is still available. Say that the listing
is currently marked available in the database and that
a property consultant can confirm the latest status.

If the customer asks about negotiation, do not confirm
that the price is negotiable. A property consultant can
advise on the seller's terms.

Keep the WhatsApp reply concise and natural.
"""

        final_response = (
            openai_client.responses.create(

                model="gpt-5.6-luna",

                instructions=final_instructions,

                input=conversation_history
            )
        )

        ai_reply = final_response.output_text.strip()

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


# =========================================================
# LOCAL RUN
# =========================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=10000
    )
