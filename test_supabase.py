import asyncio
import httpx

SUPABASE_URL = "https://qqkxxquqdxyiqalhccmo.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InFxa3h4cXVxZHh5aXFhbGhjY21vIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzYzODI4OTIsImV4cCI6MjA5MTk1ODg5Mn0.R6Lkyf3SRchy1Tl7w05msRYAsvvGlf5ELJohUeUi_E8"
HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}

async def test():
    async with httpx.AsyncClient() as client:
        # Sin filtro activo
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/horarios_profesional",
            headers=HEADERS,
            params={
                "profesional_id": "eq.3b90bf47-16be-4116-b348-fd1bf2b9ef8c",
                "select": "dia_semana,hora_inicio,hora_fin,duracion_slot_min,activo"
            }
        )
        print("Horarios sin filtro:", r.json())

asyncio.run(test())
