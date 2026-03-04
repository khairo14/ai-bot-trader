import asyncio
import httpx

async def test():
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get("http://localhost:8000/api/portfolio/summary")
        data = r.json()
        for b in data["brokers"]:
            print(f"{b['broker']:10}  connected={b['connected']}  total={b['total']} {b['currency']}  paper={b['is_paper']}")
        print()
        print("open_positions:", data["open_positions"])
        print("today_pnl:     ", data["today_pnl"])

asyncio.run(test())
