import os
import asyncio
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()
client = AsyncOpenAI()

async def test():
    try:
        response = await client.chat.completions.create(
            model="gpt-5.5",
            messages=[{"role": "user", "content": "Hello"}],
            max_completion_tokens=250,
            timeout=30.0,
        )
        print("Success:", response.choices[0].message.content)
    except Exception as e:
        print("Error type:", type(e).__name__)
        print("Error message:", e)

asyncio.run(test())
