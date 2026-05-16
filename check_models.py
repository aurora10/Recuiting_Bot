import os
from dotenv import load_dotenv
import openai

load_dotenv()
client = openai.OpenAI()

try:
    models = client.models.list()
    gpt_models = [m.id for m in models if 'gpt-5' in m.id or 'gpt-5.5' in m.id or 'gpt-4' in m.id]
    print("Found GPT models:", sorted(gpt_models))
except Exception as e:
    print("Error:", e)
