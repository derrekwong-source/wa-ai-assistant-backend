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

LISTING RULES:

1. Only recommend listings when the customer's property type,
   location and budget are all known.

2. If any of these three required search fields are missing,
   DO NOT search for listings.

3. If any of these three required search fields are missing,
   DO NOT recommend any specific property.

4. If any required search field is missing, naturally ask for
   the missing information.

5. Ask only one or two questions at a time.

6. If matching listings are provided, use ONLY those listings.

7. Do not make up property information.

8. Do not invent prices, sizes, locations or availability.

9. If a matching listing exists, you may introduce it naturally.

10. If no matching listing exists, tell the customer that there
    is currently no suitable listing found in the available
    database and that a property consultant can assist.

11. Do not promise discounts.

12. Do not negotiate prices.

13. Do not give legal, tax or loan advice.

14. Do not claim that a property is available unless its
    property_status indicates it is available.

15. The database listing status is not guaranteed to be
    real-time availability. If the customer asks whether
    a property is still available, explain that the listing
    is currently marked as available in the database but
    a property consultant should confirm the latest status.

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

    payload["last_message_at"] = (
        datetime.now(timezone.utc).isoformat()
    )

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


def parse_money(value):
    """
    Convert common Malaysian price formats into numeric RM.

    Examples:
    RM3 million -> 3000000
    RM3m -> 3000000
    RM2.5m -> 2500000
    RM800k -> 800000
    """

    if not value:
        return None

    text = (
        str(value)
        .lower()
        .replace(",", "")
        .replace(" ", "")
    )

    match = re.search(
        r"(\d+(?:\.\d+)?)\s*(million|m|k)?",
        text
    )

    if not match:
        return None

    number = float(match.group(1))
    unit = match.group(2)

    if unit == "million" or unit == "m":
        number *= 1_000_000

    elif unit == "k":
        number *= 1_000

    return number


def get_budget_limit(budget):
    """
    Try to determine the customer's maximum budget.

    Examples:
    RM3m -> 3000000
    RM2m - RM3m -> 3000000
    below RM3m -> 3000000
    """

    if not budget:
        return None

    text = (
        str(budget)
        .lower()
        .replace(",", "")
    )

    matches = re.findall(
        r"\d+(?:\.\d+)?\s*(?:million|m|k)?",
        text
    )

    values = []

    for item in matches:
        value = parse_money(item)

        if value:
            values.append(value)

    if not values:
        return None

    return max(values)


def get_matching_listings(profile):
    """
    Search the listings table using the customer's
    known location and property type.
    """

    url = f"{SUPABASE_URL}/rest/v1/listings"

    params = {
        "select": "*",
        "limit": "20",
        "order": "created_at.desc"
    }

    location = profile.get("location")
    property_type = profile.get("property_type")

    if location:
        params["location"] = f"ilike.*{location}*"

    if property_type:
        params["property_type"] = (
            f"ilike.*{property_type}*"
        )

    response = requests.get(
        url,
        headers=supabase_headers(),
        params=params,
        timeout=15
    )

    response.raise_for_status()

    listings = response.json()

    budget_limit = get_budget_limit(
        profile.get("budget")
    )

    filtered_listings = []

    for listing in listings:

        status = str(
            listing.get("property_status") or ""
        ).lower()

        if status in [
            "sold",
            "rented",
            "withdrawn",
            "inactive"
        ]:
            continue

        asking_price = parse_money(
            listing.get("asking_price")
        )

        if budget_limit and asking_price:

            if asking_price > budget_limit:
                continue

        filtered_listings.append(listing)

    return filtered_listings


def format_listings_for_ai(listings):

    if not listings:
        return "NO MATCHING LISTINGS FOUND."

    output = []

    for listing in listings:

        output.append({
            "listing_id": listing.get("listing_id"),
            "property_type": listing.get("property_type"),
            "location": listing.get("location"),
            "land_area": listing.get("land_area"),
            "built_up": listing.get("built_up"),
            "asking_price": listing.get("asking_price"),
            "tenure": listing.get("tenure"),
            "property_status": listing.get(
                "property_status"
            ),
            "suitable_for": listing.get(
                "suitable_for"
            ),
            "description": listing.get(
                "description"
            )
        })

    return json.dumps(
        output,
        ensure_ascii=False
    )


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


@app.route("/webhook", methods=["POST"])
def webhook():

    data = request.get_json()

    print(
        "Incoming WhatsApp message:",
        data
    )

    try:

        value = data[
            "entry"
        ][0][
            "changes"
        ][0][
            "value"
        ]

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
        customer_message = message[
            "text"
        ]["body"]

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

        # --------------------------------------------------
        # FIND OR CREATE CUSTOMER
        # --------------------------------------------------

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

        # --------------------------------------------------
        # SAVE CUSTOMER MESSAGE
        # --------------------------------------------------

        save_message(
            customer_id,
            "customer",
            customer_message,
            whatsapp_message_id
        )

        # --------------------------------------------------
        # LOAD CONVERSATION HISTORY
        # --------------------------------------------------

        conversation_history = (
            get_conversation_history(
                customer_id
            )
        )

        # --------------------------------------------------
        # FIRST AI PASS
        # EXTRACT CUSTOMER PROFILE
        # --------------------------------------------------

        profile_response = (
            openai_client.responses.create(

                model="gpt-5.6-luna",

                instructions="""
You are extracting customer property-search
information from a WhatsApp conversation.

Extract only information clearly provided
by the customer.

Do not guess.

If a field is unknown, return null.

For budget, preserve the customer's wording.

For intent:
- Own Stay
- Investment
- null

For lead_status:
- New Lead

unless the conversation clearly indicates
a more advanced lead.
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

        # --------------------------------------------------
        # UPDATE CUSTOMER PROFILE
        # --------------------------------------------------

        update_customer(
            customer_id,
            customer_profile
        )

        # --------------------------------------------------
        # CHECK IF LISTING SEARCH IS READY
        # --------------------------------------------------

        property_type = (
            customer_profile.get(
                "property_type"
            )
        )

        location = (
            customer_profile.get(
                "location"
            )
        )

        budget = (
            customer_profile.get(
                "budget"
            )
        )

        search_ready = bool(
            property_type
            and location
            and budget
        )

        print(
            "Search ready:",
            search_ready
        )

        # --------------------------------------------------
        # SEARCH LISTINGS ONLY WHEN REQUIREMENTS
        # ARE COMPLETE
        # --------------------------------------------------

        if search_ready:

            matching_listings = (
                get_matching_listings(
                    customer_profile
                )
            )

            listings_context = (
                format_listings_for_ai(
                    matching_listings
                )
            )

        else:

            matching_listings = []

            listings_context = """
LISTING SEARCH NOT PERFORMED.

The customer's property search requirements
are incomplete.

Do NOT recommend any listing.

Do NOT mention any specific property.

The required information for listing search is:

- Property type
- Location
- Budget

Ask naturally for the missing information.

Ask only one or two questions at a time.
"""

        print(
            "Matching Listings:",
            matching_listings
        )

        # --------------------------------------------------
        # SECOND AI PASS
        # GENERATE CUSTOMER REPLY
        # --------------------------------------------------

        response = (
            openai_client.responses.create(

                model="gpt-5.6-luna",

                instructions=(
                    SYSTEM_INSTRUCTIONS
                    + f"""

CUSTOMER PROFILE:

{json.dumps(
    customer_profile,
    ensure_ascii=False
)}


MATCHING LISTINGS FROM DATABASE:

{listings_context}


IMPORTANT:

FIRST PRIORITY — INCOMPLETE REQUIREMENTS

If the customer's property type,
location or budget is missing:

DO NOT recommend any listing.

DO NOT mention any specific property.

DO NOT say that no suitable listing
was found.

Instead, naturally ask for the missing
information.

Ask only one or two questions at a time.

Examples:

"Sure 😊 Which area are you looking for,
and what's your budget?"

"Sure 😊 Which area are you looking for?
And is this for your own business
or investment?"


SECOND PRIORITY — COMPLETE REQUIREMENTS

Only when property type, location and
budget are all known should you use
the listing database results.

If matching listings are provided:

- Use only the information provided.
- You may mention one or two suitable listings.
- Do not invent missing details.
- Do not change the listed asking price.
- Do not claim availability beyond
  the property_status.
- If appropriate, ask whether the customer
  would like more details.

If NO MATCHING LISTINGS FOUND:

- Do not invent a property.
- Tell the customer that no suitable
  listing was found in the current database.
- A property consultant can assist
  with other options.

Keep the WhatsApp reply short,
friendly and natural.
"""
                ),

                input=conversation_history,

                text={
                    "format": {

                        "type": "json_schema",

                        "name": "customer_reply",

                        "schema": {

                            "type": "object",

                            "properties": {

                                "reply": {
                                    "type": "string"
                                },

                                "profile": {

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
                                }
                            },

                            "required": [
                                "reply",
                                "profile"
                            ],

                            "additionalProperties": False
                        },

                        "strict": True
                    }
                }
            )
        )

        result = json.loads(
            response.output_text
        )

        ai_reply = result["reply"]

        customer_profile = result[
            "profile"
        ]

        print(
            "AI Reply:",
            ai_reply
        )

        print(
            "Customer Profile:",
            customer_profile
        )

        # --------------------------------------------------
        # UPDATE CUSTOMER PROFILE AGAIN
        # --------------------------------------------------

        update_customer(
            customer_id,
            customer_profile
        )

        # --------------------------------------------------
        # SAVE AI REPLY
        # --------------------------------------------------

        save_message(
            customer_id,
            "ai",
            ai_reply
        )

        # --------------------------------------------------
        # SEND REPLY THROUGH WHATSAPP
        # --------------------------------------------------

        url = (
            f"https://graph.facebook.com/v26.0/"
            f"{WHATSAPP_PHONE_NUMBER_ID}/messages"
        )

        headers = {
            "Authorization": (
                f"Bearer {WHATSAPP_ACCESS_TOKEN}"
            ),
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

        print(
            "ERROR:",
            str(e)
        )

    return "EVENT_RECEIVED", 200


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
