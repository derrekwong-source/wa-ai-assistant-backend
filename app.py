import os
import re
import requests
from flask import Flask, request, jsonify, Response
from openai import OpenAI
from pydantic import BaseModel
from typing import Optional
from html import escape

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

DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD")

MODEL = "gpt-5.6-luna"

client = OpenAI(api_key=OPENAI_API_KEY)

SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

LEAD_STATUSES = [
    "New Lead",
    "Warm Lead",
    "Hot Lead",
    "Contacted",
    "Viewing",
    "Negotiation",
    "Closed",
    "Lost",
]


# =========================================================
# CUSTOMER PROFILE
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
# BASIC ROUTE
# =========================================================

@app.route("/", methods=["GET"])
def home():
    return "WA AI Assistant is running."


# =========================================================
# WEBHOOK VERIFICATION
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
# DASHBOARD AUTHENTICATION
# =========================================================

def dashboard_authenticated():

    auth = request.authorization

    if not auth:
        return False

    if auth.username != "admin":
        return False

    if not DASHBOARD_PASSWORD:
        return False

    return auth.password == DASHBOARD_PASSWORD


def dashboard_auth_required():

    return Response(
        "Dashboard login required.",
        401,
        {
            "WWW-Authenticate": (
                'Basic realm="WA AI Assistant Dashboard"'
            )
        },
    )


# =========================================================
# CUSTOMER FUNCTIONS
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
        print(
            "Supabase get customer error:",
            response.text,
        )
        return None

    data = response.json()

    return data[0] if data else None


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
        print(
            "Supabase create customer error:",
            response.text,
        )
        return None

    data = response.json()

    return data[0] if data else None


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
        print(
            "Supabase update customer error:",
            response.text,
        )
        return None

    if response.status_code == 204:
        return True

    data = response.json()

    return data[0] if data else True


# =========================================================
# GET HOT LEADS / DASHBOARD CUSTOMERS
# =========================================================

def get_dashboard_leads():

    url = f"{SUPABASE_URL}/rest/v1/customers"

    params = {
        "select": "*",
        "order": "last_message_at.desc.nullslast,created_at.desc",
        "limit": "100",
    }

    response = requests.get(
        url,
        headers=SUPABASE_HEADERS,
        params=params,
        timeout=20,
    )

    if response.status_code != 200:
        print(
            "Supabase dashboard leads error:",
            response.status_code,
            response.text,
        )
        return []

    return response.json()


# =========================================================
# UPDATE LEAD STATUS FROM DASHBOARD
# =========================================================

@app.route("/dashboard/update-status", methods=["POST"])
def dashboard_update_status():

    if not dashboard_authenticated():
        return dashboard_auth_required()

    data = request.get_json(
        silent=True
    ) or {}

    customer_id = data.get(
        "customer_id"
    )

    new_status = data.get(
        "lead_status"
    )

    if not customer_id:
        return jsonify({
            "success": False,
            "message": "Missing customer_id",
        }), 400

    if new_status not in LEAD_STATUSES:
        return jsonify({
            "success": False,
            "message": "Invalid lead status",
        }), 400

    try:

        customer_id = int(customer_id)

    except (TypeError, ValueError):

        return jsonify({
            "success": False,
            "message": "Invalid customer_id",
        }), 400

    updates = {
        "lead_status": new_status,
    }

    # Once agent manually changes status,
    # human handoff is no longer pending.
    if new_status in [
        "Contacted",
        "Viewing",
        "Negotiation",
        "Closed",
        "Lost",
    ]:
        updates["handoff_required"] = False

    # If manually moved back to Hot Lead,
    # mark it as requiring human attention.
    elif new_status == "Hot Lead":
        updates["handoff_required"] = True

    result = update_customer(
        customer_id,
        updates,
    )

    if result is None:
        return jsonify({
            "success": False,
            "message": "Unable to update customer",
        }), 500

    return jsonify({
        "success": True,
        "lead_status": new_status,
        "handoff_required": updates.get(
            "handoff_required"
        ),
    }), 200


# =========================================================
# DASHBOARD
# =========================================================

@app.route("/dashboard", methods=["GET"])
def dashboard():

    if not dashboard_authenticated():
        return dashboard_auth_required()

    leads = get_dashboard_leads()

    hot_count = 0
    warm_count = 0
    contact_count = 0
    viewing_count = 0
    negotiation_count = 0
    closed_count = 0
    lost_count = 0

    for lead in leads:

        status = lead.get(
            "lead_status"
        )

        if status == "Hot Lead":
            hot_count += 1

        elif status == "Warm Lead":
            warm_count += 1

        elif status == "Contacted":
            contact_count += 1

        elif status == "Viewing":
            viewing_count += 1

        elif status == "Negotiation":
            negotiation_count += 1

        elif status == "Closed":
            closed_count += 1

        elif status == "Lost":
            lost_count += 1

    rows = ""

    for lead in leads:

        customer_id = lead.get(
            "id"
        )

        name = escape(
            str(
                lead.get("name")
                or "Unknown"
            )
        )

        phone = escape(
            str(
                lead.get("whatsapp_phone")
                or "-"
            )
        )

        intent = escape(
            str(
                lead.get("intent")
                or "-"
            )
        )

        budget = escape(
            str(
                lead.get("budget")
                or "-"
            )
        )

        location = escape(
            str(
                lead.get("location")
                or "-"
            )
        )

        property_type = escape(
            str(
                lead.get("property_type")
                or "-"
            )
        )

        interested_property = escape(
            str(
                lead.get("interested_property")
                or "-"
            )
        )

        lead_status = (
            lead.get("lead_status")
            or "New Lead"
        )

        last_message_at = escape(
            str(
                lead.get("last_message_at")
                or lead.get("created_at")
                or "-"
            )
        )

        options = ""

        for status in LEAD_STATUSES:

            selected = (
                "selected"
                if status == lead_status
                else ""
            )

            options += (
                f'<option value="{escape(status)}" '
                f'{selected}>{escape(status)}</option>'
            )

        status_class = "status-default"

        if lead_status == "Hot Lead":
            status_class = "status-hot"

        elif lead_status == "Warm Lead":
            status_class = "status-warm"

        elif lead_status == "Contacted":
            status_class = "status-contacted"

        elif lead_status == "Viewing":
            status_class = "status-viewing"

        elif lead_status == "Negotiation":
            status_class = "status-negotiation"

        elif lead_status == "Closed":
            status_class = "status-closed"

        elif lead_status == "Lost":
            status_class = "status-lost"

        rows += f"""
        <tr>

            <td>
                <strong>{name}</strong><br>
                <span class="phone">{phone}</span>
            </td>

            <td>{intent}</td>

            <td>
                <strong>{budget}</strong>
            </td>

            <td>{location}</td>

            <td>{property_type}</td>

            <td class="property">
                {interested_property}
            </td>

            <td>

                <select
                    class="status-select {status_class}"
                    data-customer-id="{customer_id}"
                    onchange="updateStatus(this)"
                >

                    {options}

                </select>

            </td>

            <td>
                <span class="handoff">
                    {"YES" if lead.get("handoff_required") else "NO"}
                </span>
            </td>

            <td class="date">
                {last_message_at}
            </td>

        </tr>
        """

    if not rows:

        rows = """
        <tr>
            <td colspan="9" class="empty">
                No customers yet.
            </td>
        </tr>
        """

    html = f"""
<!DOCTYPE html>

<html>

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>WA AI Assistant - Dashboard</title>

<style>

* {{
    box-sizing: border-box;
}}

body {{
    margin: 0;
    padding: 0;
    background: #f4f6f8;
    color: #172033;
    font-family:
        Arial,
        Helvetica,
        sans-serif;
}}

.header {{
    background: #101d3a;
    color: white;
    padding: 22px 28px;
}}

.header-inner {{
    max-width: 1600px;
    margin: auto;
    display: flex;
    justify-content: space-between;
    align-items: center;
}}

.logo {{
    font-size: 22px;
    font-weight: 700;
}}

.subtitle {{
    margin-top: 5px;
    color: #c8d1df;
    font-size: 13px;
}}

.refresh {{
    font-size: 12px;
    color: #d8dee8;
}}

.container {{
    max-width: 1600px;
    margin: 28px auto;
    padding: 0 20px;
}}

.stats {{
    display: grid;
    grid-template-columns:
        repeat(7, minmax(120px, 1fr));
    gap: 12px;
    margin-bottom: 22px;
}}

.stat-card {{
    background: white;
    border-radius: 12px;
    padding: 16px;
    box-shadow:
        0 2px 10px rgba(0,0,0,0.06);
}}

.stat-title {{
    font-size: 11px;
    color: #718096;
    margin-bottom: 7px;
    font-weight: 700;
}}

.stat-number {{
    font-size: 25px;
    font-weight: 700;
}}

.stat-hot {{
    color: #d12f2f;
}}

.stat-warm {{
    color: #c77b00;
}}

.stat-viewing {{
    color: #2463a8;
}}

.stat-negotiation {{
    color: #7542a8;
}}

.stat-closed {{
    color: #23844d;
}}

.stat-lost {{
    color: #6b7280;
}}

.table-wrapper {{
    background: white;
    border-radius: 12px;
    overflow-x: auto;
    box-shadow:
        0 2px 10px rgba(0,0,0,0.06);
}}

table {{
    width: 100%;
    border-collapse: collapse;
    min-width: 1450px;
}}

th {{
    background: #101d3a;
    color: white;
    text-align: left;
    padding: 14px 12px;
    font-size: 12px;
    white-space: nowrap;
}}

td {{
    padding: 15px 12px;
    border-bottom: 1px solid #edf0f3;
    vertical-align: top;
    font-size: 13px;
}}

tr:hover {{
    background: #fafbfc;
}}

.phone {{
    color: #64748b;
    font-size: 12px;
}}

.property {{
    max-width: 360px;
    line-height: 1.5;
}}

.status-select {{
    min-width: 135px;
    padding: 7px 9px;
    border-radius: 7px;
    border: 1px solid #d5dae1;
    font-size: 12px;
    font-weight: 700;
    cursor: pointer;
    background: white;
}}

.status-hot {{
    color: #d12f2f;
    border-color: #f0b5b5;
}}

.status-warm {{
    color: #a56b00;
    border-color: #e8c875;
}}

.status-contacted {{
    color: #2868a5;
}}

.status-viewing {{
    color: #2463a8;
    border-color: #a9c9e8;
}}

.status-negotiation {{
    color: #7542a8;
    border-color: #cbb5df;
}}

.status-closed {{
    color: #23844d;
    border-color: #a9d6ba;
}}

.status-lost {{
    color: #6b7280;
}}

.status-default {{
    color: #475569;
}}

.handoff {{
    display: inline-block;
    background: #fff6df;
    color: #a56b00;
    padding: 5px 9px;
    border-radius: 6px;
    font-weight: 700;
    font-size: 11px;
}}

.date {{
    color: #64748b;
    white-space: nowrap;
    font-size: 11px;
}}

.empty {{
    text-align: center;
    padding: 60px;
    color: #718096;
}}

@media (max-width: 1000px) {{

    .stats {{
        grid-template-columns:
            repeat(3, 1fr);
    }}

}}

@media (max-width: 700px) {{

    .header-inner {{
        display: block;
    }}

    .refresh {{
        margin-top: 8px;
    }}

    .stats {{
        grid-template-columns:
            repeat(2, 1fr);
    }}

}}

</style>

</head>

<body>

<div class="header">

    <div class="header-inner">

        <div>

            <div class="logo">
                WA AI Assistant
            </div>

            <div class="subtitle">
                Lead Management Dashboard
            </div>

        </div>

        <div class="refresh">
            Auto refresh: 30 seconds
        </div>

    </div>

</div>


<div class="container">

    <div class="stats">

        <div class="stat-card">
            <div class="stat-title">
                HOT
            </div>
            <div class="stat-number stat-hot">
                {hot_count}
            </div>
        </div>

        <div class="stat-card">
            <div class="stat-title">
                WARM
            </div>
            <div class="stat-number stat-warm">
                {warm_count}
            </div>
        </div>

        <div class="stat-card">
            <div class="stat-title">
                CONTACTED
            </div>
            <div class="stat-number">
                {contact_count}
            </div>
        </div>

        <div class="stat-card">
            <div class="stat-title">
                VIEWING
            </div>
            <div class="stat-number stat-viewing">
                {viewing_count}
            </div>
        </div>

        <div class="stat-card">
            <div class="stat-title">
                NEGOTIATION
            </div>
            <div class="stat-number stat-negotiation">
                {negotiation_count}
            </div>
        </div>

        <div class="stat-card">
            <div class="stat-title">
                CLOSED
            </div>
            <div class="stat-number stat-closed">
                {closed_count}
            </div>
        </div>

        <div class="stat-card">
            <div class="stat-title">
                LOST
            </div>
            <div class="stat-number stat-lost">
                {lost_count}
            </div>
        </div>

    </div>


    <div class="table-wrapper">

        <table>

            <thead>

                <tr>

                    <th>Customer</th>
                    <th>Intent</th>
                    <th>Budget</th>
                    <th>Location</th>
                    <th>Property Type</th>
                    <th>Interested Property</th>
                    <th>Lead Status</th>
                    <th>Handoff</th>
                    <th>Last Message</th>

                </tr>

            </thead>

            <tbody>

                {rows}

            </tbody>

        </table>

    </div>

</div>


<script>

async function updateStatus(selectElement) {{

    const customerId =
        selectElement.dataset.customerId;

    const newStatus =
        selectElement.value;

    selectElement.disabled = true;

    try {{

        const response = await fetch(
            "/dashboard/update-status",
            {{
                method: "POST",
                headers: {{
                    "Content-Type":
                        "application/json"
                }},
                body: JSON.stringify({{
                    customer_id:
                        customerId,
                    lead_status:
                        newStatus
                }})
            }}
        );

        const result =
            await response.json();

        if (!result.success) {{

            alert(
                "Unable to update status: "
                + result.message
            );

            window.location.reload();

            return;
        }}

        window.location.reload();

    }} catch (error) {{

        alert(
            "Connection error. "
            + "Please try again."
        );

        window.location.reload();

    }}

}}

</script>

</body>

</html>
"""

    return Response(
        html,
        mimetype="text/html",
    )


# =========================================================
# SAVE MESSAGE
# =========================================================

def save_message(
    customer_id,
    sender,
    message,
    whatsapp_message_id=None,
):

    url = f"{SUPABASE_URL}/rest/v1/messages"

    payload = {
        "customer_id": customer_id,
        "sender": sender,
        "message": message,
    }

    if whatsapp_message_id:
        payload["whatsapp_message_id"] = (
            whatsapp_message_id
        )

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
        print(
            "Supabase save message error:",
            response.text,
        )


# =========================================================
# AGENT SETTINGS
# =========================================================

def get_agent_settings():

    url = f"{SUPABASE_URL}/rest/v1/agent_settings"

    params = {
        "select": "*",
        "notification_enabled": "eq.true",
        "limit": "1",
    }

    response = requests.get(
        url,
        headers=SUPABASE_HEADERS,
        params=params,
        timeout=20,
    )

    if response.status_code != 200:
        print(
            "Supabase agent settings error:",
            response.status_code,
            response.text,
        )
        return None

    data = response.json()

    return data[0] if data else None


# =========================================================
# LISTING FUNCTIONS
# =========================================================

def parse_price(value):

    if value is None:
        return None

    text = (
        str(value)
        .lower()
        .replace(",", "")
        .replace("rm", "")
        .strip()
    )

    multiplier = 1

    if "million" in text or "mil" in text:
        multiplier = 1_000_000

    elif "k" in text:
        multiplier = 1_000

    numbers = re.findall(
        r"\d+(?:\.\d+)?",
        text,
    )

    if not numbers:
        return None

    return float(numbers[0]) * multiplier


def parse_budget(value):

    if value is None:
        return None

    text = (
        str(value)
        .lower()
        .replace(",", "")
        .replace("rm", "")
        .strip()
    )

    multiplier = 1

    if "million" in text or "mil" in text:
        multiplier = 1_000_000

    elif "k" in text:
        multiplier = 1_000

    numbers = re.findall(
        r"\d+(?:\.\d+)?",
        text,
    )

    if not numbers:
        return None

    return float(numbers[0]) * multiplier


def search_listings(
    location=None,
    property_type=None,
    budget=None,
):

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
        print(
            "Supabase listings error:",
            response.text,
        )
        return []

    listings = response.json()

    customer_budget = parse_budget(
        budget
    )

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

        if location:

            if (
                str(location).lower()
                not in listing_location
            ):
                continue

        if property_type:

            property_type_lower = (
                str(property_type).lower()
            )

            if (
                property_type_lower
                not in listing_type
                and listing_type
                not in property_type_lower
            ):
                continue

        if (
            customer_budget is not None
            and asking_price is not None
        ):

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

        output.append(
            text.strip()
        )

    return "\n\n".join(output)


# =========================================================
# CUSTOMER PROFILE EXTRACTION
# =========================================================

def extract_customer_profile(
    latest_message,
    existing_profile,
):

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

1. Keep existing information if still valid.
2. Update information when the customer provides new information.
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
8. Interested property should describe the property being discussed.
9. Do not change lead_status based only on normal enquiries.
"""

    try:

        response = client.responses.parse(
            model=MODEL,
            input=[
                {
                    "role": "system",
                    "content": (
                        "Extract structured customer "
                        "profile information accurately."
                    ),
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

        print(
            "Profile extraction error:",
            str(e),
        )

        return {
            "name": existing_profile.get("name"),
            "intent": existing_profile.get("intent"),
            "location": existing_profile.get("location"),
            "budget": existing_profile.get("budget"),
            "property_type": existing_profile.get(
                "property_type"
            ),
            "interested_property": (
                existing_profile.get(
                    "interested_property"
                )
            ),
            "lead_status": existing_profile.get(
                "lead_status"
            ),
        }


# =========================================================
# HUMAN HANDOFF DETECTION
# =========================================================

def detect_handoff(
    message,
    profile,
):

    text = message.lower().strip()

    handoff_phrases = [

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

        "arrange a viewing",
        "arrange viewing",
        "schedule a viewing",
        "book a viewing",
        "arrange viewing appointment",
        "want to view",
        "would like to view",

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

    buying_intents = [
        "buy",
        "purchase",
        "invest",
    ]

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
        and str(
            profile.get("intent")
        ).lower()
        in buying_intents
    ):

        if any(
            word in text
            for word in buying_words
        ):
            return True

    return False


# =========================================================
# AI RESPONSE
# =========================================================

def generate_ai_reply(
    latest_message,
    customer_profile,
    listings,
    handoff_required=False,
):

    listing_context = format_listings(
        listings
    )

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

This customer has requested human assistance
or has shown strong intent to proceed.

The system has flagged this customer
for human follow-up.

Tell the customer naturally that
a property consultant will follow up/contact them.

Do NOT claim a specific human has already contacted them.

Do NOT promise a specific response time.

Do NOT invent an agent name.

Keep the reply concise.
"""

    prompt = f"""
You are the first-line WhatsApp property assistant.

You are NOT the property salesperson.

Your job is to:

1. Answer the customer's latest question.
2. Use available listing information.
3. Be concise and natural.
4. Never invent property information.
5. Never promise discounts.
6. Never promise availability unless the database says Available.
7. If the customer asks about negotiation,
   say the asking price is the listed price
   and negotiation depends on the seller.
8. If the customer asks about viewing,
   say a property consultant can assist.
9. If information is unavailable,
   say a property consultant can confirm it.
10. Do not repeat unnecessary information.
11. Focus primarily on the latest customer message.

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
                        "You are a professional Malaysian "
                        "property WhatsApp first-line assistant."
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

        print(
            "AI response error:",
            str(e),
        )

        return (
            "Thanks for your message. "
            "A property consultant will "
            "assist you further."
        )


# =========================================================
# SEND WHATSAPP
# =========================================================

def send_whatsapp_message(
    to_phone,
    message,
):

    url = (
        f"https://graph.facebook.com/v23.0/"
        f"{WHATSAPP_PHONE_NUMBER_ID}/messages"
    )

    headers = {
        "Authorization": (
            f"Bearer {WHATSAPP_ACCESS_TOKEN}"
        ),
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
# HOT LEAD NOTIFICATION
# =========================================================

def send_hot_lead_notification(
    customer,
    profile,
    agent_settings,
):

    if not agent_settings:

        print(
            "No active agent notification setting found."
        )

        return False

    agent_phone = agent_settings.get(
        "whatsapp_phone"
    )

    if not agent_phone:

        print(
            "Agent notification phone is empty."
        )

        return False

    customer_phone = customer.get(
        "whatsapp_phone"
    )

    name = (
        profile.get("name")
        or customer.get("name")
        or "Unknown"
    )

    intent = (
        profile.get("intent")
        or customer.get("intent")
        or "Not specified"
    )

    location = (
        profile.get("location")
        or customer.get("location")
        or "Not specified"
    )

    budget = (
        profile.get("budget")
        or customer.get("budget")
        or "Not specified"
    )

    property_type = (
        profile.get("property_type")
        or customer.get("property_type")
        or "Not specified"
    )

    interested_property = (
        profile.get("interested_property")
        or customer.get("interested_property")
        or "Not specified"
    )

    notification = f"""🔥 HOT LEAD

Customer: {name}
WhatsApp: {customer_phone}

Intent: {intent}
Budget: {budget}
Location: {location}
Property Type: {property_type}

Interested Property:
{interested_property}

⚠️ Customer requires human follow-up.

Please contact the customer directly."""

    print(
        "Sending Hot Lead notification to:",
        agent_phone,
    )

    return send_whatsapp_message(
        to_phone=agent_phone,
        message=notification,
    )


# =========================================================
# WHATSAPP WEBHOOK
# =========================================================

@app.route("/webhook", methods=["POST"])
def webhook():

    data = request.get_json(
        silent=True
    )

    print("Incoming webhook:")
    print(data)

    try:

        entry = data.get("entry", [])

        for entry_item in entry:

            changes = entry_item.get(
                "changes",
                [],
            )

            for change in changes:

                value = change.get(
                    "value",
                    {},
                )

                messages = value.get(
                    "messages",
                    [],
                )

                for message in messages:

                    message_type = message.get(
                        "type"
                    )

                    if message_type != "text":
                        continue

                    whatsapp_message_id = (
                        message.get("id")
                    )

                    sender = message.get(
                        "from"
                    )

                    text_body = (
                        message
                        .get("text", {})
                        .get("body", "")
                        .strip()
                    )

                    if not sender or not text_body:
                        continue

                    print(
                        "===================================="
                    )

                    print(
                        "Customer:",
                        sender,
                    )

                    print(
                        "Message:",
                        text_body,
                    )

                    customer = get_customer(
                        sender
                    )

                    if not customer:

                        customer = create_customer(
                            sender
                        )

                        if not customer:

                            print(
                                "Unable to create customer."
                            )

                            continue

                    customer_id = customer.get(
                        "id"
                    )

                    print(
                        "Customer ID:",
                        customer_id,
                    )

                    save_message(
                        customer_id=customer_id,
                        sender="customer",
                        message=text_body,
                        whatsapp_message_id=(
                            whatsapp_message_id
                        ),
                    )

                    profile = (
                        extract_customer_profile(
                            latest_message=text_body,
                            existing_profile=customer,
                        )
                    )

                    print(
                        "Customer Profile:",
                        profile,
                    )

                    old_handoff_required = bool(
                        customer.get(
                            "handoff_required"
                        ) or False
                    )

                    handoff_triggered = (
                        detect_handoff(
                            message=text_body,
                            profile=profile,
                        )
                    )

                    handoff_required = (
                        old_handoff_required
                        or handoff_triggered
                    )

                    existing_lead_status = (
                        customer.get(
                            "lead_status"
                        )
                    )

                    lead_status = (
                        existing_lead_status
                        or profile.get(
                            "lead_status"
                        )
                        or "New Lead"
                    )

                    if handoff_triggered:

                        lead_status = "Hot Lead"

                    elif (
                        existing_lead_status
                        == "Hot Lead"
                    ):

                        lead_status = "Hot Lead"

                    customer_updates = {
                        "name": profile.get(
                            "name"
                        ),
                        "intent": profile.get(
                            "intent"
                        ),
                        "location": profile.get(
                            "location"
                        ),
                        "budget": profile.get(
                            "budget"
                        ),
                        "property_type": profile.get(
                            "property_type"
                        ),
                        "interested_property": (
                            profile.get(
                                "interested_property"
                            )
                        ),
                        "lead_status": lead_status,
                        "handoff_required": (
                            handoff_required
                        ),
                    }

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

                    matching_listings = (
                        search_listings(
                            location=profile.get(
                                "location"
                            ),
                            property_type=profile.get(
                                "property_type"
                            ),
                            budget=profile.get(
                                "budget"
                            ),
                        )
                    )

                    print(
                        "Matching Listings:",
                        matching_listings,
                    )

                    combined_profile = {
                        **customer,
                        **profile,
                        "lead_status": lead_status,
                    }

                    ai_reply = (
                        generate_ai_reply(
                            latest_message=text_body,
                            customer_profile=(
                                combined_profile
                            ),
                            listings=matching_listings,
                            handoff_required=(
                                handoff_required
                            ),
                        )
                    )

                    print(
                        "AI Reply:",
                        ai_reply,
                    )

                    save_message(
                        customer_id=customer_id,
                        sender="assistant",
                        message=ai_reply,
                    )

                    send_whatsapp_message(
                        to_phone=sender,
                        message=ai_reply,
                    )

                    if (
                        handoff_triggered
                        and not old_handoff_required
                    ):

                        agent_settings = (
                            get_agent_settings()
                        )

                        print(
                            "Agent Settings:",
                            agent_settings,
                        )

                        send_hot_lead_notification(
                            customer=customer,
                            profile=profile,
                            agent_settings=(
                                agent_settings
                            ),
                        )

        return jsonify({
            "status": "ok"
        }), 200

    except Exception as e:

        print(
            "Webhook error:",
            str(e),
        )

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
            5000,
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
    )
