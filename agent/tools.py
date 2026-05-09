# agent/tools.py — Herramientas del agente
# Conectado a Supabase del consultorio Data y Gestión

"""
Herramientas específicas del consultorio Data y Gestión.
Casos de uso: FAQ + Agendar turnos con consulta a Supabase.
"""

import os
import json
import yaml
import logging
import httpx
from datetime import datetime, date, timedelta
from typing import Optional

logger = logging.getLogger("agentkit")

# ─── Configuración Supabase ───────────────────────────────────────────────────
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://qqkxxquqdxyiqalhccmo.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InFxa3h4cXVxZHh5aXFhbGhjY21vIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzYzODI4OTIsImV4cCI6MjA5MTk1ODg5Mn0.R6Lkyf3SRchy1Tl7w05msRYAsvvGlf5ELJohUeUi_E8")

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

DIAS_SEMANA = {
    0: "Lunes",
    1: "Martes",
    2: "Miércoles",
    3: "Jueves",
    4: "Viernes",
    5: "Sábado",
    6: "Domingo",
}

# día_semana en DB: 1=Lunes, 2=Martes... 7=Domingo (estilo ISO)
DIA_NOMBRE_A_NUM = {
    "lunes": 1, "martes": 2, "miércoles": 3, "miercoles": 3,
    "jueves": 4, "viernes": 5, "sábado": 6, "sabado": 6, "domingo": 7,
}


def cargar_info_negocio() -> dict:
    """Carga la información del negocio desde business.yaml."""
    try:
        with open("config/business.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        return {}


# ─── Supabase helpers ─────────────────────────────────────────────────────────

async def supabase_get(tabla: str, params: dict = None) -> list:
    """Hace un GET a Supabase REST API."""
    url = f"{SUPABASE_URL}/rest/v1/{tabla}"
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=HEADERS, params=params or {})
        if r.status_code == 200:
            return r.json()
        logger.error(f"Supabase GET error {r.status_code}: {r.text}")
        return []


async def supabase_post(tabla: str, data: dict) -> dict:
    """Hace un POST a Supabase REST API."""
    url = f"{SUPABASE_URL}/rest/v1/{tabla}"
    async with httpx.AsyncClient() as client:
        r = await client.post(url, headers=HEADERS, json=data)
        if r.status_code in (200, 201):
            resultado = r.json()
            return resultado[0] if isinstance(resultado, list) else resultado
        logger.error(f"Supabase POST error {r.status_code}: {r.text}")
        return {}


# ─── Herramientas de consulta ─────────────────────────────────────────────────

async def obtener_especialidades() -> list[str]:
    """Retorna las especialidades únicas de los profesionales activos."""
    profesionales = await supabase_get("profesionales", {"activo": "eq.true", "select": "especialidad"})
    especialidades = list({p["especialidad"].strip() for p in profesionales if p.get("especialidad")})
    return sorted(especialidades)


async def obtener_profesionales_por_especialidad(especialidad: str) -> list[dict]:
    """Retorna los profesionales activos de una especialidad dada."""
    todos = await supabase_get("profesionales", {
        "activo": "eq.true",
        "select": "id,nombre,apellido,especialidad"
    })
    return [
        p for p in todos
        if especialidad.lower() in p.get("especialidad", "").lower()
    ]


async def obtener_horarios_profesional(profesional_id: str) -> list[dict]:
    """Retorna los horarios activos de un profesional."""
    horarios = await supabase_get("horarios_profesional", {
        "profesional_id": f"eq.{profesional_id}",
        "activo": "eq.true",
        "select": "dia_semana,hora_inicio,hora_fin,duracion_slot_min"
    })
    return horarios


async def obtener_turnos_ocupados(profesional_id: str, fecha: str) -> list[str]:
    """Retorna los horarios ya ocupados de un profesional en una fecha."""
    turnos = await supabase_get("turnos", {
        "profesional_id": f"eq.{profesional_id}",
        "fecha": f"eq.{fecha}",
        "estado": "not.in.(rechazado)",
        "select": "hora_inicio,hora_fin"
    })
    return [(t["hora_inicio"], t["hora_fin"]) for t in turnos]


async def obtener_slots_disponibles(profesional_id: str, fecha_str: str) -> list[str]:
    """
    Calcula los slots disponibles para un profesional en una fecha dada.
    fecha_str: "YYYY-MM-DD"
    """
    try:
        fecha = datetime.strptime(fecha_str, "%Y-%m-%d").date()
    except ValueError:
        return []

    # día ISO: lunes=1, domingo=7
    dia_iso = fecha.isoweekday()

    horarios = await obtener_horarios_profesional(profesional_id)
    horario_dia = next((h for h in horarios if h["dia_semana"] == dia_iso), None)

    if not horario_dia:
        return []

    ocupados = await obtener_turnos_ocupados(profesional_id, fecha_str)
    ocupados_inicio = {t[0][:5] for t in ocupados}  # "HH:MM"

    # Generar slots
    inicio = datetime.strptime(horario_dia["hora_inicio"][:5], "%H:%M")
    fin = datetime.strptime(horario_dia["hora_fin"][:5], "%H:%M")
    duracion = timedelta(minutes=horario_dia["duracion_slot_min"])

    slots = []
    actual = inicio
    while actual + duracion <= fin:
        slot_str = actual.strftime("%H:%M")
        if slot_str not in ocupados_inicio:
            slots.append(slot_str)
        actual += duracion

    return slots


async def obtener_proximas_fechas_disponibles(profesional_id: str, dias_a_buscar: int = 14) -> list[dict]:
    """
    Busca los próximos días con slots disponibles para un profesional.
    Retorna lista de {fecha, dia_nombre, slots}
    """
    horarios = await obtener_horarios_profesional(profesional_id)
    dias_con_horario = {h["dia_semana"] for h in horarios}

    resultados = []
    hoy = date.today()

    for i in range(1, dias_a_buscar + 1):
        fecha = hoy + timedelta(days=i)
        dia_iso = fecha.isoweekday()

        if dia_iso not in dias_con_horario:
            continue

        fecha_str = fecha.strftime("%Y-%m-%d")
        slots = await obtener_slots_disponibles(profesional_id, fecha_str)

        if slots:
            resultados.append({
                "fecha": fecha_str,
                "dia_nombre": DIAS_SEMANA[fecha.weekday()],
                "slots": slots[:6]  # máx 6 slots para no saturar el mensaje
            })

        if len(resultados) >= 3:  # mostrar máx 3 fechas
            break

    return resultados


async def obtener_obras_sociales() -> list[str]:
    """Retorna las obras sociales activas."""
    os_list = await supabase_get("obras_sociales", {"activo": "eq.true", "select": "nombre"})
    return [o["nombre"] for o in os_list]


# ─── Registrar turno ──────────────────────────────────────────────────────────

async def registrar_turno_supabase(
    profesional_id: str,
    fecha: str,
    hora_inicio: str,
    duracion_min: int,
    nombre: str,
    apellido: str,
    telefono: str,
    motivo: str,
    obra_social: Optional[str] = None,
    dni: Optional[str] = None,
    email: Optional[str] = None,
) -> dict:
    """
    Registra un turno directamente en Supabase.
    Calcula hora_fin según duración del slot del profesional.
    """
    # Calcular hora_fin
    hi = datetime.strptime(hora_inicio, "%H:%M")
    hf = hi + timedelta(minutes=duracion_min)
    hora_fin = hf.strftime("%H:%M:%S")
    hora_inicio_full = hi.strftime("%H:%M:%S")

    cobertura = obra_social if obra_social and obra_social.lower() != "particular" else None

    turno = {
        "profesional_id": profesional_id,
        "fecha": fecha,
        "hora_inicio": hora_inicio_full,
        "hora_fin": hora_fin,
        "motivo_consulta": motivo,
        "estado": "confirmado",
        "origen": "whatsapp",
        "nombre_solicitante": nombre,
        "apellido_solicitante": apellido,
        "telefono_solicitante": telefono,
        "requiere_validacion": False,
        "es_sobreturno": False,
    }

    if dni:
        turno["dni_solicitante"] = dni
    if email:
        turno["email_solicitante"] = email

    resultado = await supabase_post("turnos", turno)

    if resultado.get("id"):
        logger.info(f"Turno registrado en Supabase: {resultado['id']}")
        return {"ok": True, "id": resultado["id"]}
    else:
        logger.error(f"Error registrando turno: {resultado}")
        return {"ok": False}


# ─── Horario del consultorio ──────────────────────────────────────────────────

def obtener_horario() -> dict:
    """Retorna si el consultorio está abierto ahora."""
    ahora = datetime.now()
    dia_semana = ahora.weekday()
    hora = ahora.hour

    if dia_semana <= 4:
        esta_abierto = 9 <= hora < 19
    elif dia_semana == 5:
        esta_abierto = 9 <= hora < 13
    else:
        esta_abierto = False

    return {
        "horario": "Lunes a Viernes 9-19hs, Sábados 9-13hs",
        "esta_abierto": esta_abierto,
        "dia_actual": DIAS_SEMANA[dia_semana],
    }
