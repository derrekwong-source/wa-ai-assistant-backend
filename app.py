import os
import re
import requests
from flask import Flask, request, jsonify
from openai import OpenAI
from pydantic import BaseModel, Field
from typing import Optional, List

app = Flask(__name__)

# =========================================================
# ENV
# =========================================================

WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

MODEL = "gpt-5.6-luna"

client = OpenAI(api_key=OPENAI_API_KEY)


# =========================================================
# SUPABASE HEADERS
# =========================================================

SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}


# =========================================================
# CUSTOMER PROFILE STRUCTURE
# =========================================================

class CustomerProfile(BaseModel):
    name: Optional[str] = None
    intent: Optional[str] = None
    location: Optional[str] = None
    budget: Optional[str] = None
    property_type: Optional[str] = None
    interested_property: Optional[str] = None
    lead_status: Optional[str] = None


# =========================================================
# BASIC ROUTES
# =========================================================

@app.route("/", methods=["GET"])
def home():
    return "WA AI Assistant is running."


# =========================================================
# WHATSAPP WEBHOOK VERIFICATION
# =========================================================

@app.route("/webhook", methods=["GET"])
def verify_webhook():

    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200

    return "Verification failed", 403


# =========================================================
# SUPABASE CUSTOMER FUNCTIONS
# =========================================================

def get_customer(phone):
    url = f"{SUPABASE_URL}/rest/v1/customers"

    params = {
        "whatsapp_phone": f"eq.{phone}",
        "select": "*",
        "limit": "1",
    }

    response = requests.get(
        url,
        headers=SUPABASE_HEADERS,
        params=params,
        timeout=20,
    )

    if response.status_code != 200:
        print("Supabase get customer error:", response.text)
        return None

    data = response.json()

    if not data:
        return None

    return data[0]


def create_customer(phone):
    url = f"{SUPABASE_URL}/rest/v1/customers"

    payload = {
        "whatsapp_phone": phone,
        "lead_status": "New Lead",
        "handoff_required": False,
    }

    response = requests.post(
        url,
        headers={
            **SUPABASE_HEADERS,
            "Prefer": "return=representation",
        },
        json=payload,
        timeout=20,
    )

    if response.status_code not in [200, 201]:
        print("Supabase create customer error:", response.text)
        return None

    data = response.json()

    if not data:
        return None

    return data[0]


def update_customer(customer_id, updates):
    url = f"{SUPABASE_URL}/rest/v1/customers"

    params = {
        "id": f"eq.{customer_id}",
    }

    response = requests.patch(
        url,
        headers={
            **SUPABASE_HEADERS,
            "Prefer": "return=representation",
        },
        params=params,
        json=updates,
        timeout=20,
    )

    if response.status_code not in [200, 204]:
        print("Supabase update customer error:", response.text)
        return None

    if response.status_code == 204:
        return True

    return response.json()


# =========================================================
# SAVE MESSAGE
# =========================================================

def save_message(customer_id, sender, message, whatsapp_message_id=None):

    url = f"{SUPABASE_URL}/rest/v1/messages"

    payload = {
        "customer_id": customer_id,
        "sender": sender,
        "message": message,
    }

    if whatsapp_message_id:
        payload["whatsapp_message_id"] = whatsapp_message_id

    response = requests.post(
        url,
        headers={
            **SUPABASE_HEADERS,
            "Prefer": "return=minimal",
        },
        json=payload,
        timeout=20,
    )

    if response.status_code not in [200, 201]:
        print("Supabase save message error:", response.text)


# =========================================================
# LISTING FUNCTIONS
# =========================================================

def parse_price(value):
    """
    Convert listing asking_price into a numeric value.
    Examples:
    3,000,000
    RM3,000,000
    RM 3 million
    """

    if value is None:
        return None

    text = str(value).lower().replace(",", "").replace("rm", "").strip()

    multiplier = 1

    if "million" in text or "mil" in text:
        multiplier = 1_000_000
    elif "k" in text:
        multiplier = 1_000

    numbers = re.findall(r"\d+(?:\.\d+)?", text)

    if not numbers:
        return None

    return float(numbers[0]) * multiplier


def parse_budget(value):
    """
    Convert customer budget into numeric value.
    """

    if value is None:
        return None

    text = str(value).lower().replace(",", "").replace("rm", "").strip()

    multiplier = 1

    if "million" in text or "mil" in text:
        multiplier = 1_000_000
    elif "k" in text:
        multiplier = 1_000

    numbers = re.findall(r"\d+(?:\.\d+)?", text)

    if not numbers:
        return None

    return float(numbers[0]) * multiplier


def search_listings(location=None, property_type=None, budget=None):

    url = f"{SUPABASE_URL}/rest/v1/listings"

    params = {
        "select": "*",
        "property_status": "eq.Available",
        "limit": "50",
    }

    response = requests.get(
        url,
        headers=SUPABASE_HEADERS,
        params=params,
        timeout=20,
    )

    if response.status_code != 200:
        print("Supabase listings error:", response.text)
        return []

    listings = response.json()

    customer_budget = parse_budget(budget)

    results = []

    for listing in listings:

        listing_location = str(
            listing.get("location") or ""
        ).lower()

        listing_type = str(
            listing.get("property_type") or ""
        ).lower()

        asking_price = parse_price(
            listing.get("asking_price")
        )

        # Location filter
        if location:
            if str(location).lower() not in listing_location:
                continue

        # Property type filter
        if property_type:
            property_type_lower = str(property_type).lower()

            if (
                property_type_lower not in listing_type
                and listing_type not in property_type_lower
            ):
                continue

        # Budget filter
        if customer_budget is not None and asking_price is not None:

            if asking_price > customer_budget:
                continue

        results.append(listing)

    return results


def format_listings(listings):

    if not listings:
        return "No matching listings found."

    output = []

    for listing in listings:

        text = f"""
Listing ID: {listing.get('listing_id')}
Property Type: {listing.get('property_type')}
Location: {listing.get('location')}
Land Area: {listing.get('land_area')}
Built-up: {listing.get('built_up')}
Asking Price: RM{listing.get('asking_price')}
Tenure: {listing.get('tenure')}
Status: {listing.get('property_status')}
Suitable For: {listing.get('suitable_for')}
Description: {listing.get('description')}
"""

        output.append(text.strip())

    return "\n\n".join(output)


# =========================================================
# CUSTOMER PROFILE EXTRACTION
# =========================================================

def extract_customer_profile(latest_message, existing_profile):

    existing_text = f"""
Existing customer profile:

Name: {existing_profile.get('name')}
Intent: {existing_profile.get('intent')}
Location: {existing_profile.get('location')}
Budget: {existing_profile.get('budget')}
Property Type: {existing_profile.get('property_type')}
Interested Property: {existing_profile.get('interested_property')}
Lead Status: {existing_profile.get('lead_status')}
"""

    prompt = f"""
You are extracting a WhatsApp property customer's profile.

{existing_text}

Latest customer message:
{latest_message}

Update the profile using the latest message.

Rules:

1. Keep existing information if it is still valid.
2. If the customer provides new information, update it.
3. Do not invent information.
4. Budget should remain as the customer's stated budget.
5. Intent can be:
   - Buy
   - Rent
   - Sell
   - Invest
   - Enquiry
   - Other
6. Property type should only be filled when reasonably clear.
7. Location should only be filled when reasonably clear.
8. Interested property should describe the property the customer appears to be discussing.
9. Do not change lead_status based only on normal enquiries.
"""

    try:

        response = client.responses.parse(
            model=MODEL,
            input=[
                {
                    "role": "system",
                    "content": "Extract structured customer profile information accurately.",
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            text_format=CustomerProfile,
        )

        profile = response.output_parsed

        return profile.model_dump()

    except Exception as e:

        print("Profile extraction error:", str(e))

        return {
            "name": existing_profile.get("name"),
            "intent": existing_profile.get("intent"),
            "location": existing_profile.get("location"),
            "budget": existing_profile.get("budget"),
            "property_type": existing_profile.get("property_type"),
            "interested_property": existing_profile.get(
                "interested_property"
            ),
            "lead_status": existing_profile.get("lead_status"),
        }


# =========================================================
# HUMAN HANDOFF DETECTION
# =========================================================

def detect_handoff(message, profile):

    text = message.lower().strip()

    handoff_phrases = [

        # Direct request for agent
        "ask the agent to contact me",
        "ask agent to contact me",
        "agent contact me",
        "agent call me",
        "agent whatsapp me",
        "ask the agent",
        "contact me",
        "call me",
        "please contact me",
        "please call me",

        # Ready to proceed
        "ready to proceed",
        "ready to buy",
        "ready to purchase",
        "i want to buy",
        "i want to purchase",
        "want to buy this",
        "want to purchase this",
        "i will buy",
        "i'm buying",
        "im buying",
        "proceed with purchase",
        "proceed with this property",

        # Viewing / appointment
        "arrange a viewing",
        "arrange viewing",
        "schedule a viewing",
        "book a viewing",
        "arrange viewing appointment",
        "want to view",
        "would like to view",

        # Negotiation / serious buyer
        "make an offer",
        "i want to make an offer",
        "submit an offer",
        "negotiate with owner",
        "negotiate with seller",
        "talk to the owner",
        "talk to seller",
    ]

    for phrase in handoff_phrases:

        if phrase in text:
            return True

    # If profile intent clearly indicates buying and
    # the customer is asking to proceed, also trigger handoff.
    buying_words = [
        "buy",
        "purchase",
        "proceed",
        "offer",
        "viewing",
        "view",
    ]

    if (
        profile.get("intent")
        and str(profile.get("intent")).lower() in [
            "buy",
            "purchase",
            "invest",
        ]
    ):

        if any(word in text for word in buying_words):
            return True

    return False


# =========================================================
# FINAL AI RESPONSE
# =========================================================

def generate_ai_reply(
    latest_message,
    customer_profile,
    listings,
    handoff_required=False,
):

    listing_context = format_listings(listings)

    profile_context = f"""
Customer Profile:

Name: {customer_profile.get('name')}
Intent: {customer_profile.get('intent')}
Location: {customer_profile.get('location')}
Budget: {customer_profile.get('budget')}
Property Type: {customer_profile.get('property_type')}
Interested Property: {customer_profile.get('interested_property')}
Lead Status: {customer_profile.get('lead_status')}
"""

    handoff_instruction = ""

    if handoff_required:

        handoff_instruction = """
IMPORTANT:

This customer has requested human assistance or has shown strong intent to proceed.

The system has already flagged this customer for human follow-up.

Reply naturally and professionally.

Tell the customer that a property consultant will follow up/contact them.

Do NOT claim that a specific human has already contacted them.

Do NOT promise a specific response time.

Do NOT invent an agent name.

Keep the reply concise.
"""

    prompt = f"""
You are the first-line WhatsApp property assistant.

You are NOT the property salesperson.

Your job is to:

1. Answer the customer's latest question.
2. Use the available listing information.
3. Be concise and natural.
4. Never invent property information.
5. Never promise discounts.
6. Never promise availability unless the database says Available.
7. If the customer asks about negotiation, say the asking price is the listed price and negotiation depends on the seller.
8. If the customer asks about viewing, say a property consultant can assist.
9. If information is not available, say that a property consultant can confirm it.
10. Do not repeat unnecessary information.
11. Focus primarily on the customer's latest message.

{handoff_instruction}

{profile_context}

Available Listings:

{listing_context}

Latest Customer Message:

{latest_message}

Write the WhatsApp reply now.
"""

    try:

        response = client.responses.create(
            model=MODEL,
            input=[
                {
                    "role": "system",
                    "content": (
                        "You are a professional Malaysian property "
                        "WhatsApp first-line assistant."
                    ),
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
        )

        return response.output_text.strip()

    except Exception as e:

        print("AI response error:", str(e))

        return (
            "Thanks for your message. A property consultant will "
            "assist you further."
        )


# =========================================================
# SEND WHATSAPP MESSAGE
# =========================================================

def send_whatsapp_message(to_phone, message):

    url = (
        f"https://graph.facebook.com/v23.0/"
        f"{WHATSAPP_PHONE_NUMBER_ID}/messages"
    )

    headers = {
        "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {
            "body": message
        },
    }

    response = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=20,
    )

    if response.status_code not in [200, 201]:
        print(
            "WhatsApp send error:",
            response.status_code,
            response.text,
        )

        return False

    return True


# =========================================================
# WHATSAPP WEBHOOK
# =========================================================

@app.route("/webhook", methods=["POST"])
def webhook():

    data = request.get_json(silent=True)

    print("Incoming webhook:")
    print(data)

    try:

        entry = data.get("entry", [])

        for entry_item in entry:

            changes = entry_item.get("changes", [])

            for change in changes:

                value = change.get("value", {})

                messages = value.get("messages", [])

                for message in messages:

                    message_type = message.get("type")

                    # Only process text messages
                    if message_type != "text":
                        continue

                    whatsapp_message_id = message.get("id")

                    sender = message.get("from")

                    text_body = (
                        message
                        .get("text", {})
                        .get("body", "")
                        .strip()
                    )

                    if not sender or not text_body:
                        continue

                    print("====================================")
                    print("Customer:", sender)
                    print("Message:", text_body)

                    # -------------------------------------------------
                    # GET OR CREATE CUSTOMER
                    # -------------------------------------------------

                    customer = get_customer(sender)

                    if not customer:

                        customer = create_customer(sender)

                        if not customer:
                            print("Unable to create customer.")
                            continue

                    customer_id = customer.get("id")

                    print("Customer ID:", customer_id)

                    # -------------------------------------------------
                    # SAVE INCOMING MESSAGE
                    # -------------------------------------------------

                    save_message(
                        customer_id=customer_id,
                        sender="customer",
                        message=text_body,
                        whatsapp_message_id=whatsapp_message_id,
                    )

                    # -------------------------------------------------
                    # EXTRACT CUSTOMER PROFILE
                    # -------------------------------------------------

                    profile = extract_customer_profile(
                        latest_message=text_body,
                        existing_profile=customer,
                    )

                    print("Customer Profile:", profile)

                    # -------------------------------------------------
                    # DETECT HUMAN HANDOFF
                    # -------------------------------------------------

                    handoff_triggered = detect_handoff(
                        message=text_body,
                        profile=profile,
                    )

                    # Existing handoff flag
                    existing_handoff = bool(
                        customer.get("handoff_required") or False
                    )

                    handoff_required = (
                        existing_handoff
                        or handoff_triggered
                    )

                    # -------------------------------------------------
                    # LEAD STATUS
                    # -------------------------------------------------

                    existing_lead_status = customer.get(
                        "lead_status"
                    )

                    lead_status = (
                        existing_lead_status
                        or profile.get("lead_status")
                        or "New Lead"
                    )

                    if handoff_triggered:

                        lead_status = "Hot Lead"

                    elif existing_lead_status == "Hot Lead":

                        lead_status = "Hot Lead"

                    # -------------------------------------------------
                    # UPDATE CUSTOMER
                    # -------------------------------------------------

                    customer_updates = {
                        "name": profile.get("name"),
                        "intent": profile.get("intent"),
                        "location": profile.get("location"),
                        "budget": profile.get("budget"),
                        "property_type": profile.get(
                            "property_type"
                        ),
                        "interested_property": profile.get(
                            "interested_property"
                        ),
                        "lead_status": lead_status,
                        "handoff_required": handoff_required,
                        "last_message_at": "now()",
                    }

                    # Supabase REST API does not evaluate now()
                    # inside JSON, so remove it and let DB default
                    # behaviour be handled separately.
                    customer_updates.pop("last_message_at")

                    update_customer(
                        customer_id,
                        customer_updates,
                    )

                    print(
                        "Handoff Required:",
                        handoff_required,
                    )

                    print(
                        "Lead Status:",
                        lead_status,
                    )

                    # -------------------------------------------------
                    # SEARCH LISTINGS
                    # -------------------------------------------------

                    matching_listings = search_listings(
                        location=profile.get("location"),
                        property_type=profile.get(
                            "property_type"
                        ),
                        budget=profile.get("budget"),
                    )

                    print(
                        "Matching Listings:",
                        matching_listings,
                    )

                    # -------------------------------------------------
                    # GENERATE AI RESPONSE
                    # -------------------------------------------------

                    ai_reply = generate_ai_reply(
                        latest_message=text_body,
                        customer_profile={
                            **customer,
                            **profile,
                            "lead_status": lead_status,
                        },
                        listings=matching_listings,
                        handoff_required=handoff_required,
                    )

                    print("AI Reply:", ai_reply)

                    # -------------------------------------------------
                    # SAVE AI MESSAGE
                    # -------------------------------------------------

                    save_message(
                        customer_id=customer_id,
                        sender="assistant",
                        message=ai_reply,
                    )

                    # -------------------------------------------------
                    # SEND WHATSAPP
                    # -------------------------------------------------

                    send_whatsapp_message(
                        to_phone=sender,
                        message=ai_reply,
                    )

        return jsonify({
            "status": "ok"
        }), 200

    except Exception as e:

        print("Webhook error:", str(e))

        return jsonify({
            "status": "error",
            "message": str(e),
        }), 200


# =========================================================
# START SERVER
# =========================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            5000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
    )
