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

1. Remember everything the customer has already told you.
2. The LATEST information provided by the customer always
   overrides older information.
3. If the customer changes their budget, location, property type,
   intent, or other requirement, immediately use the new information.
4. Never continue using an old value after the customer has
   clearly updated it.
5. Do NOT ask the same question again if the customer has already answered it.
6. Have a natural conversation, one or two questions at a time.
7. Do not ask all qualification questions at once.
8. Use information already provided by the customer.

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

4. If the customer updates one of these fields,
   immediately use the updated value for the next search.

5. The customer's current budget is authoritative.
   Never recommend a property above the current budget.

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
    real-time availability.

16. If the customer asks whether a property is still available,
    explain that the listing is currently marked as available
    in the database but a property consultant should confirm
    the latest status.

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

    if unit in ["million", "m"]:
        number *= 1_000_000

    elif unit == "k":
        number *= 1_000

    return number


def get_budget_limit(budget):

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


def extract_latest_budget(message):

    if not message:
        return None

    text = str(message)

    pattern = re.search(
        r"""
        (?:
            budget
            |
            my\s+budget
        )
        \s*
        (?:
            is
            |
            now
            |
            :
        )?
        \s*
        (?:RM\s*)?
        (
            \d+(?:\.\d+)?
        )
        \s*
        (
            million
            |
            m
            |
            k
        )?
        """,
        text,
        re.IGNORECASE | re.VERBOSE
    )

    if not pattern:
        return None

    number = pattern.group(1)
    unit = pattern.group(2)

    if not number:
        return None

    if unit:
        return f"RM{number} {unit}"

    return f"RM{number}"


def get_matching_listings(profile):

    url = f"{SUPABASE_URL}/rest/v1/listings"

    params = {
        "select": "*",
        "limit": "20",
        "order": "created_at.desc"
    }

    location = profile.get("location")
    property_type = profile.get("property_type")

    if location:
        params["location"] = (
            f"ilike.*{location}*"
        )

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
            "listing_id": listing.get(
                "listing_id"
            ),
            "property_type": listing.get(
                "property_type"
            ),
            "location": listing.get(
                "location"
            ),
            "land_area": listing.get(
                "land_area"
            ),
            "built_up": listing.get(
                "built_up"
            ),
            "asking_price": listing.get(
                "asking_price"
            ),
            "tenure": listing.get(
                "tenure"
            ),
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

    mode = request.args.get(
        "hub.mode"
    )

    token = request.args.get(
        "hub.verify_token"
    )

    challenge = request.args.get(
        "hub.challenge"
    )

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

        customer_message = (
            message["text"]["body"]
        )

        whatsapp_message_id = (
            message.get("id")
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
        # LOAD HISTORY
        # --------------------------------------------------

        conversation_history = (
            get_conversation_history(
                customer_id
            )
        )

        # --------------------------------------------------
        # EXTRACT CUSTOMER PROFILE
        # --------------------------------------------------

        profile_response = (
            openai_client.responses.create(

                model="gpt-5.6-luna",

                instructions="""
Extract the customer's CURRENT property-search profile.

The latest customer message has priority over
all previous messages.

If the customer changes a value, replace the
old value with the new value.

Examples:

Previous:
Budget RM3 million

Latest:
My budget is RM2.5 million.

Current budget:
RM2.5 million

Previous:
Puchong

Latest:
I'm now looking in Shah Alam.

Current location:
Shah Alam

Do not keep outdated values when the customer
clearly provides a new value.

Do not guess missing information.

For budget, use the latest value provided
by the customer.

For intent:
- Own Stay
- Investment
- null

For lead_status:
- New Lead
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

        # --------------------------------------------------
        # FORCE LATEST EXPLICIT BUDGET
        # --------------------------------------------------

        latest_budget = (
            extract_latest_budget(
                customer_message
            )
        )

        budget_was_updated = False

        if latest_budget:

            old_budget = customer_profile.get(
                "budget"
            )

            customer_profile["budget"] = (
                latest_budget
            )

            budget_was_updated = True

            print(
                "Budget updated:",
                old_budget,
                "->",
                latest_budget
            )

        print(
            "Customer Profile:",
            customer_profile
        )

        # --------------------------------------------------
        # UPDATE DATABASE
        # --------------------------------------------------

        update_customer(
            customer_id,
            customer_profile
        )

        # --------------------------------------------------
        # SEARCH REQUIREMENTS
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
        # SEARCH LISTINGS
        # --------------------------------------------------

        if search_ready:

            matching_listings = (
                get_matching_listings(
                    customer_profile
                )
            )

        else:

            matching_listings = []

        print(
            "Matching Listings:",
            matching_listings
        )

        # --------------------------------------------------
        # IMPORTANT:
        # IF CUSTOMER JUST UPDATED BUDGET AND THERE IS
        # NO MATCH, DO NOT LET THE AI USE OLD LISTINGS
        # --------------------------------------------------

        if budget_was_updated and not matching_listings:

            ai_reply = (
                f"Thanks 😊 I’ve updated your budget "
                f"to {latest_budget}. "
                f"Currently, I don’t have a suitable "
                f"listing within this budget in our "
                f"available database. "
                f"A property consultant can assist "
                f"with other options."
            )

        elif not search_ready:

            missing_fields = []

            if not property_type:
                missing_fields.append(
                    "property type"
                )

            if not location:
                missing_fields.append(
                    "location"
                )

            if not budget:
                missing_fields.append(
                    "budget"
                )

            if len(missing_fields) == 1:

                ai_reply = (
                    "Sure 😊 Could you let me know "
                    f"your {missing_fields[0]}?"
                )

            else:

                ai_reply = (
                    "Sure 😊 Could you let me know "
                    + " and ".join(
                        missing_fields
                    )
                    + "?"
                )

        else:

            # --------------------------------------------------
            # FORMAT CURRENT LISTINGS ONLY
            # --------------------------------------------------

            listings_context = (
                format_listings_for_ai(
                    matching_listings
                )
            )

            # --------------------------------------------------
            # GENERATE REPLY USING CURRENT LISTINGS ONLY
            # --------------------------------------------------

            response = (
                openai_client.responses.create(

                    model="gpt-5.6-luna",

                    instructions=(
                        SYSTEM_INSTRUCTIONS
                        + f"""

CURRENT CUSTOMER PROFILE:

{json.dumps(
    customer_profile,
    ensure_ascii=False
)}


CURRENT DATABASE MATCHES:

{listings_context}


IMPORTANT:

The listings above are the ONLY listings
you may recommend.

Do NOT use an older property from the
conversation if it is not included above.

The customer's CURRENT budget is:

{budget}

Never recommend a property above that budget.

If the customer has just changed their
requirements, use the current database
matches only.

Keep the reply short and natural.
"""
                    ),

                    input=[
                        {
                            "role": "user",
                            "content": customer_message
                        }
                    ],

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

            final_profile = result[
                "profile"
            ]

            # Never allow an older budget to overwrite
            # the latest budget.

            if latest_budget:

                final_profile["budget"] = (
                    latest_budget
                )

            update_customer(
                customer_id,
                final_profile
            )

        print(
            "AI Reply:",
            ai_reply
        )

        # --------------------------------------------------
        # SAVE AI MESSAGE
        # --------------------------------------------------

        save_message(
            customer_id,
            "ai",
            ai_reply
        )

        # --------------------------------------------------
        # SEND WHATSAPP MESSAGE
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
