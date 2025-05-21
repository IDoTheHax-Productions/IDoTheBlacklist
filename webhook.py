from flask import Flask, request
import aiohttp
import asyncio
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

@app.route("/bunq-webhook", methods=["POST"])
async def bunq_webhook():
    try:
        data = request.get_json()
        if not data:
            logger.error("No JSON data received in webhook")
            return "", 400
        logger.info(f"Received webhook data: {data}")
        # Forward to bot's internal endpoint
        async with aiohttp.ClientSession() as session:
            async with session.post("http://localhost:8080/bunq-webhook", json=data) as resp:
                if resp.status != 200:
                    logger.error(f"Failed to forward webhook to bot: {resp.status} {await resp.text()}")
                    return "", 500
                logger.info("Webhook forwarded successfully")
                return "", 200
    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        return "", 500

if __name__ == "__main__":
    app.run(port=5000)