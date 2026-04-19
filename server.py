from flask import Flask, request, jsonify

app = Flask(__name__)


@app.route("/")
def home():
    return "FRIDAY Server Running 🚀"


@app.route("/ask", methods=["POST"])
def ask():
    payload = request.get_json(silent=True) or {}
    user_message = payload.get("message")
    reply = f"FRIDAY: {user_message}"
    return jsonify({"reply": reply})


if __name__ == "__main__":
    # Run the Flask development server on port 5000
    app.run(debug=True, host="0.0.0.0", port=5000)
