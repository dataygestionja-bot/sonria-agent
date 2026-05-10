# agent/tools.py — Herramientas del agente
# Conectado a Supabase del consultorio Data y Gestión

import os
import yaml
import logging
import httpx
from datetime import datetime, date, timedelta
from typing import Optional

logger = logging.getLogger("agentkit")

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://qqkxxquqdxyiqalhccmo.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InFxa3h4cXVxZHh5aXFhbGhjY21vIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzYzODI4OTIsImV4cCI6MjA5MTk1ODg5Mn0.R6Lkyf3SRchy1Tl7w05msRYAsvvGlf5ELJohUeUi_E8")

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

DIAS_SEMANA = {
    0: "Lunes", 1: "Martes", 2: "Miercoles", 3: "Jueves",
    4: "Viernes", 5: "Sabado", 6: "Domingo",
}


def cargar_info_negocio() -> dict:
    try:
        with open("config/business.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        return {}


# ─── Supabase helpers ─────────────────────────────────────────────────────────

async def supabase_get(tabla: str, params: dict = None) -> list:
    url = f"{SUPABASE_URL}/rest/v1/{tabla}"
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=HEADERS, params=params or {})
        if r.status_code == 200:
            return r.json()
        logger.error(f"Supabase GET error {r.status_code}: {r.text}")
        return []


async def supabase_post(tabla: str, data: dict) -> dict:
    url = f"{SUPABASE_URL}/rest/v1/{tabla}"
    async with httpx.AsyncClient() as client:
        r = await client.post(url, headers=HEADERS, json=data)
        if r.status_code in (200, 201):
            resultado = r.json()
            return resultado[0] if isinstance(resultado, list) else resultado
        logger.error(f"Supabase POST error {r.status_code}: {r.text}")
        return {}


# ─── Pacientes ────────────────────────────────────────────────────────────────

async def buscar_paciente_por_dni(dni: str) -> dict | None:
    """Busca un paciente por DNI. Retorna el paciente o None."""
    resultados = await supabase_get("pacientes", {
        "dni": f"eq.{dni.strip()}",
        "activo": "eq.true",
        "select": "id,nombre,apellido,dni,telefono,obra_social_id"
    })
    return resultados[0] if resultados else None


async def crear_paciente(
    nombre: str,
    apellido: str,
    dni: str,
    telefono: str,
    obra_social_id: Optional[str] = None,
) -> dict:
    """Crea un nuevo paciente en Supabase."""
    paciente = {
        "nombre": nombre.strip().capitalize(),
        "apellido": apellido.strip().capitalize(),
        "dni": dni.strip(),
        "telefono": telefono,
        "activo": True,
        "pendiente_validacion": False,
    }
    if obra_social_id:
        paciente["obra_social_id"] = obra_social_id

    resultado = await supabase_post("pacientes", paciente)
    if resultado.get("id"):
        logger.info(f"Paciente creado: {resultado['id']}")
    return resultado


async def buscar_obra_social_id(nombre_obra: str) -> str | None:
    """Busca el ID de una obra social por nombre."""
    resultados = await supabase_get("obras_sociales", {
        "nombre": f"ilike.*{nombre_obra}*",
        "activo": "eq.true",
        "select": "id,nombre"
    })
    return resultados[0]["id"] if resultados else None


async def obtener_o_crear_paciente(
    nombre: str,
    apellido: str,
    dni: str,
    telefono: str,
    obra_social: Optional[str] = None,
) -> dict | None:
    """
    Busca el paciente por DNI. Si no existe, lo crea.
    Retorna el paciente con su ID.
    """
    # Buscar primero
    paciente = await buscar_paciente_por_dni(dni)
    if paciente:
        logger.info(f"Paciente encontrado: {paciente['id']}")
        return paciente

    # Buscar obra social ID si se proporcionó
    obra_social_id = None
    if obra_social and obra_social.lower() != "particular":
        obra_social_id = await buscar_obra_social_id(obra_social)

    # Crear paciente nuevo
    nuevo = await crear_paciente(nombre, apellido, dni, telefono, obra_social_id)
    return nuevo if nuevo.get("id") else None


# ─── Disponibilidad ───────────────────────────────────────────────────────────

async def obtener_especialidades() -> list[str]:
    profesionales = await supabase_get("profesionales", {"activo": "eq.true", "select": "especialidad"})
    especialidades = list({p["especialidad"].strip() for p in profesionales if p.get("especialidad")})
    return sorted(especialidades)


async def obtener_profesionales_por_especialidad(especialidad: str) -> list[dict]:
    todos = await supabase_get("profesionales", {
        "activo": "eq.true",
        "select": "id,nombre,apellido,especialidad"
    })
    return [p for p in todos if especialidad.lower() in p.get("especialidad", "").lower()]


async def obtener_horarios_profesional(profesional_id: str) -> list[dict]:
    horarios = await supabase_get("horarios_profesional", {
        "profesional_id": f"eq.{profesional_id}",
        "activo": "eq.true",
        "select": "dia_semana,hora_inicio,hora_fin,duracion_slot_min"
    })
    logger.info(f"Horarios {profesional_id}: {horarios} | dias_con_horario: {set(h['dia_semana'] for h in horarios)}")
    return horarios


async def obtener_turnos_ocupados(profesional_id: str, fecha: str) -> list:
    turnos = await supabase_get("turnos", {
        "profesional_id": f"eq.{profesional_id}",
        "fecha": f"eq.{fecha}",
        "estado": "neq.rechazado",
        "select": "hora_inicio,hora_fin"
    })
    return [(t["hora_inicio"], t["hora_fin"]) for t in turnos]


async def obtener_slots_disponibles(profesional_id: str, fecha_str: str) -> list[str]:
    try:
        fecha = datetime.strptime(fecha_str, "%Y-%m-%d").date()
    except ValueError:
        return []

    dia_iso = fecha.isoweekday()
    horarios = await obtener_horarios_profesional(profesional_id)
    horario_dia = next((h for h in horarios if h["dia_semana"] == dia_iso), None)

    if not horario_dia:
        return []

    ocupados = await obtener_turnos_ocupados(profesional_id, fecha_str)
    ocupados_inicio = {t[0][:5] for t in ocupados}

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
                "slots": slots[:6]
            })

        if len(resultados) >= 3:
            break

    return resultados


async def obtener_obras_sociales() -> list[str]:
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
    dni: str = "",
    obra_social: Optional[str] = None,
    email: Optional[str] = None,
) -> dict:
    """
    Busca o crea el paciente por DNI y registra el turno en Supabase.
    """
    # Obtener o crear paciente
    paciente_id = None
    if dni:
        paciente = await obtener_o_crear_paciente(nombre, apellido, dni, telefono, obra_social)
        if paciente:
            paciente_id = paciente.get("id")

    if not paciente_id:
        logger.warning("No se pudo obtener paciente_id — turno sin paciente vinculado")
        return {"ok": False, "error": "No se pudo identificar al paciente"}

    hi = datetime.strptime(hora_inicio, "%H:%M")
    hf = hi + timedelta(minutes=duracion_min)
    hora_fin = hf.strftime("%H:%M:%S")
    hora_inicio_full = hi.strftime("%H:%M:%S")

    turno = {
        "paciente_id": paciente_id,
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
        "horario": "Lunes a Viernes 9-19hs, Sabados 9-13hs",
        "esta_abierto": esta_abierto,
        "dia_actual": DIAS_SEMANA[dia_semana],
    }
