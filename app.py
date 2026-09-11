from flask import Flask, request

app = Flask(__name__)

VERIFY_TOKEN = "wa_ai_verify_2026"

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

    return "EVENT_RECEIVED", 200


@app.route("/", methods=["GET"])
def home():
    return "WA AI Assistant Backend is running", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
