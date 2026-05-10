# agent/brain.py — Cerebro del agente: conexión con Claude API
# Generado por AgentKit + integración Supabase
"""
Lógica de IA del agente. Lee el system prompt de prompts.yaml,
consulta Supabase para obtener disponibilidad real y genera respuestas
usando la API de Anthropic Claude.
"""
import os
import re
import yaml
import logging
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from agent.tools import (
    obtener_especialidades,
    obtener_profesionales_por_especialidad,
    obtener_proximas_fechas_disponibles,
    obtener_obras_sociales,
    registrar_turno_supabase,
    obtener_horario,
)

load_dotenv()
logger = logging.getLogger("agentkit")

# Cliente de Anthropic
client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# IDs de profesionales
PROFESIONALES = {
    "bruno ordoñez": "b5af188f-aa9e-4983-8365-92930cbc9eeb",
    "federico cabrera": "9cd6412e-e1e9-4b20-aa78-a9ba03ea240d",
    "florencia celsi": "318bdbf8-04dc-4953-b284-d3c5f429cbbf",
    "fernando rojas": "3b90bf47-16be-4116-b348-fd1bf2b9ef8c",
}

DURACION_SLOTS = {
    "b5af188f-aa9e-4983-8365-92930cbc9eeb": 45,  # Bruno Ordoñez
    "9cd6412e-e1e9-4b20-aa78-a9ba03ea240d": 30,  # Federico Cabrera
    "318bdbf8-04dc-4953-b284-d3c5f429cbbf": 30,  # Florencia Celsi
    "3b90bf47-16be-4116-b348-fd1bf2b9ef8c": 30,  # Fernando Rojas
}


def cargar_config_prompts() -> dict:
    """Lee toda la configuración desde config/prompts.yaml."""
    try:
        with open("config/prompts.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/prompts.yaml no encontrado")
        return {}


def cargar_system_prompt() -> str:
    config = cargar_config_prompts()
    return config.get("system_prompt", "Eres un asistente útil. Responde en español.")


def obtener_mensaje_error() -> str:
    config = cargar_config_prompts()
    return config.get("error_message", "Lo siento, estoy teniendo problemas técnicos.")


def obtener_mensaje_fallback() -> str:
    config = cargar_config_prompts()
    return config.get("fallback_message", "Disculpá, no entendí tu mensaje.")


def detectar_profesional(texto: str) -> str | None:
    """Detecta si el texto menciona algún profesional."""
    texto_lower = texto.lower()
    for nombre, pid in PROFESIONALES.items():
        # Buscar por apellido o nombre completo
        apellido = nombre.split()[-1]
        if apellido in texto_lower or nombre in texto_lower:
            return pid
    return None


def detectar_especialidad(texto: str) -> str | None:
    """Detecta si el texto menciona una especialidad."""
    texto_lower = texto.lower()
    if any(p in texto_lower for p in ["ortodoncia", "brackets", "aparatos"]):
        return "Ortodoncia"
    if any(p in texto_lower for p in ["cirugía", "cirugia", "extracción", "extraccion", "muela del juicio"]):
        return "Cirugía"
    if any(p in texto_lower for p in ["general", "limpieza", "caries", "blanqueamiento", "estética", "estetica", "implante"]):
        return "Odontología General"
    return None


async def construir_contexto_supabase(mensaje: str, historial: list[dict]) -> str:
    """
    Analiza la conversación y consulta Supabase para obtener
    información real de disponibilidad.
    Retorna un bloque de contexto para agregar al system prompt.
    """
    contexto_parts = []

    # Texto completo de la conversación para analizar
    texto_completo = mensaje.lower()
    for msg in historial[-6:]:  # últimos 6 mensajes
        texto_completo += " " + msg.get("content", "").lower()

    # ¿Se menciona algún profesional? Buscar primero en mensaje actual, luego en historial
    profesional_id = detectar_profesional(mensaje.lower()) or detectar_profesional(texto_completo)

    # ¿Se menciona alguna especialidad?
    especialidad = detectar_especialidad(texto_completo)

    # Disparar consulta si hay profesional mencionado O si se pregunta por disponibilidad
    palabras_disponibilidad = ["disponib", "fecha", "día", "dia", "horario", "turno", "cuando", "cuándo", "sábado", "sabado", "lunes", "martes", "miércoles", "miercoles", "jueves", "viernes", "quiero", "sacar", "agendar"]
    pregunta_disponibilidad = any(p in texto_completo for p in palabras_disponibilidad)

    if profesional_id:
        fechas = await obtener_proximas_fechas_disponibles(profesional_id)
        if fechas:
            lineas = []
            for f in fechas:
                slots_str = ", ".join(f["slots"])
                lineas.append(f"  • {f['dia_nombre']} {f['fecha']}: {slots_str}")
            contexto_parts.append(
                "DISPONIBILIDAD REAL (consultada ahora de la base de datos):\n" +
                "\n".join(lineas) +
                "\n⚠️ Usá EXACTAMENTE estos datos para responder. NO inventes otros horarios."
            )
        else:
            contexto_parts.append(
                "DISPONIBILIDAD REAL: No hay turnos disponibles para este profesional en los próximos 14 días."
            )

    elif especialidad and pregunta_disponibilidad:
        # Buscar profesionales de esa especialidad y sus próximas fechas
        profesionales = await obtener_profesionales_por_especialidad(especialidad)
        for prof in profesionales:
            pid = prof["id"]
            nombre_prof = f"{prof['nombre']} {prof['apellido']}"
            fechas = await obtener_proximas_fechas_disponibles(pid)
            if fechas:
                lineas = []
                for f in fechas:
                    slots_str = ", ".join(f["slots"])
                    lineas.append(f"    • {f['dia_nombre']} {f['fecha']}: {slots_str}")
                contexto_parts.append(
                    f"DISPONIBILIDAD REAL de {nombre_prof}:\n" + "\n".join(lineas)
                )

    # Agregar obras sociales si se preguntan
    if any(p in texto_completo for p in ["obra social", "obrasocial", "cobertura", "osde", "swiss", "galeno", "sancor", "osecac", "ospe"]):
        obras = await obtener_obras_sociales()
        if obras:
            contexto_parts.append(
                "OBRAS SOCIALES ACEPTADAS (datos reales): " + ", ".join(obras)
            )

    if contexto_parts:
        return "\n\n---\n🔴 INFORMACIÓN EN TIEMPO REAL DE LA BASE DE DATOS:\n" + "\n\n".join(contexto_parts) + "\n---"

    return ""


async def generar_respuesta(mensaje: str, historial: list[dict]) -> str:
    """
    Genera una respuesta usando Claude API con contexto real de Supabase.
    """
    if not mensaje or len(mensaje.strip()) < 2:
        return obtener_mensaje_fallback()

    system_prompt = cargar_system_prompt()

    # Consultar Supabase y agregar contexto real al system prompt
    try:
        contexto_real = await construir_contexto_supabase(mensaje, historial)
        if contexto_real:
            system_prompt = system_prompt + contexto_real
            logger.info("Contexto de Supabase agregado al prompt")
    except Exception as e:
        logger.error(f"Error consultando Supabase: {e}")

    # Construir mensajes para la API
    mensajes = []
    for msg in historial:
        mensajes.append({
            "role": msg["role"],
            "content": msg["content"]
        })
    mensajes.append({"role": "user", "content": mensaje})

    try:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system_prompt,
            messages=mensajes
        )
        respuesta = response.content[0].text
        logger.info(f"Respuesta generada ({response.usage.input_tokens} in / {response.usage.output_tokens} out)")
        return respuesta
    except Exception as e:
        logger.error(f"Error Claude API: {e}")
        return obtener_mensaje_error()
