# Recruitment Userbot

A fully autonomous, LLM-driven Telegram userbot designed to act as a recruitment assistant for construction workers in Belgium. The bot engages with candidates in a human-like, professional manner to collect essential profile information and work portfolios, generates a PDF dossier, and emails the complete profile to the recruitment team.

## Features

- **Natural LLM Conversation:** Powered by OpenAI (GPT-4o-mini), the bot conducts a fluid interview to gather 10 crucial data points (name, specialization, legal status, tools, location, rate, phone number, languages, team size, and availability). It automatically translates responses into professional Dutch.
- **Human-like Interaction:** Implements typing delays and conversational styles to mimic a real human recruiter.
- **Media Collection:** Autonomously handles the collection of portfolio photos and videos from candidates.
- **Automated Dossier Generation:** Uses `reportlab` to automatically generate a formatted PDF dossier of the candidate's profile, including embedded portfolio images. Fully supports Cyrillic characters.
- **Email Integration:** Sends the candidate's JSON data, PDF dossier, and media attachments directly to a designated Gmail address via SMTP.
- **Telegram Integration:** Built using `Telethon`, operating as a userbot to handle direct messages seamlessly. Also forwards the generated dossier to your Telegram "Saved Messages" (`me`).
- **Persistent Storage:** Uses a local SQLite database (`candidates.db`) to track conversation state and candidate profiles safely with WAL mode enabled.
- **Docker Ready:** Includes `Dockerfile` and `docker-compose.yml` for easy deployment and isolation.

## Prerequisites

- Python 3.9+
- A Telegram API ID and Hash (obtainable from [my.telegram.org](https://my.telegram.org))
- An OpenAI API Key
- A Gmail account with an "App Password" generated for SMTP access

## Environment Variables

Create a `.env` file in the root directory and populate it with the following required variables:

```env
API_ID=your_telegram_api_id
API_HASH=your_telegram_api_hash
PHONE=+1234567890             # Your Telegram phone number (with country code)
OPENAI_API_KEY=sk-your_openai_api_key
GMAIL_USER=your_email@gmail.com
GMAIL_PASS=your_gmail_app_password
DATA_DIR=./                   # Optional: Directory for persistent data (db, sessions, media)
```

## Setup and Installation

### Local Setup

1. Clone the repository and navigate to the root directory.
2. Create a virtual environment and activate it:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```
3. Install the dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Ensure the required font files (`DejaVuSans.ttf` and `DejaVuSans-Bold.ttf`) are present in the root directory for PDF generation.
5. Run the bot:
   ```bash
   python recruitment_userbot.py
   ```
   *Note: On the first run, you may be prompted to enter a Telegram login code sent to your Telegram app to authenticate the session.*

### Docker Deployment

You can quickly deploy the bot using Docker Compose:

1. Ensure your `.env` file is properly configured.
2. Build and start the container in detached mode:
   ```bash
   docker-compose up -d --build
   ```

## Project Structure

- `recruitment_userbot.py` - The main application script containing the Telegram event loop, LLM logic, PDF generation, and email handling.
- `requirements.txt` - Python dependencies.
- `Dockerfile` & `docker-compose.yml` - Containerization configurations.
- `candidates.db` - SQLite database for state management (generated automatically).
- `media/` & `dossiers/` - Directories for storing candidate media and generated PDFs (generated automatically).
- `recruitment_session.session` - Telethon session file.

## How It Works

1. **Intake Phase:** The bot intercepts incoming private messages. It uses an OpenAI system prompt to ask one missing piece of information at a time until all 10 fields are filled.
2. **Media Phase:** Once the text profile is complete, the bot stops asking questions and waits for the candidate to upload 3-4 photos of their work. It handles multiple media uploads correctly and waits for the user to indicate they are "done" (e.g., typing "готово").
3. **Delivery Phase:** Once the user finishes sending media, the bot generates a professional PDF dossier, zips the data and media, and sends an email to the configured `GMAIL_USER`. It also sends a confirmation message back to the candidate.
