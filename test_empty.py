import asyncio
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()
client = AsyncOpenAI()

async def test():
    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "hello"},
                {"role": "assistant", "content": ""}
            ],
            temperature=0.9,
            max_tokens=150,
            timeout=15.0,
        )
        print("Success:", response.choices[0].message.content)
    except Exception as e:
        print("Error type:", type(e).__name__)
        print("Error message:", e)

asyncio.run(test())
