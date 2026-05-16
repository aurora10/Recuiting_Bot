import asyncio
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()
client = AsyncOpenAI()

async def test():
    try:
        response = await client.chat.completions.create(
            model="gpt-5.4-mini",
            messages=[{"role": "system", "content": "hello"}],
            temperature=0.8,
            max_tokens=150,
            timeout=15.0,
        )
        print("Success max_tokens/temp:", response.choices[0].message.content)
    except Exception as e:
        print("Error with max_tokens/temp:", type(e).__name__, e)

    try:
        response = await client.chat.completions.create(
            model="gpt-5.4-mini",
            messages=[{"role": "system", "content": "hello"}],
            max_completion_tokens=150,
            timeout=15.0,
        )
        print("Success max_completion_tokens/no temp:", response.choices[0].message.content)
    except Exception as e:
        print("Error with max_completion_tokens/no temp:", type(e).__name__, e)

asyncio.run(test())
