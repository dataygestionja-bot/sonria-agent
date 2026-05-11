# agent/brain.py — Cerebro del agente: conexión con Claude API
import os
import re
import yaml
import logging
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from agent.tools import (
    obtener_profesionales_por_especialidad,
    obtener_proximas_fechas_disponibles,
    obtener_obras_sociales,
    registrar_turno_supabase,
)

load_dotenv()
logger = logging.getLogger("agentkit")

client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

PROFESIONALES = {
    "bruno ordoñez": "b5af188f-aa9e-4983-8365-92930cbc9eeb",
    "federico cabrera": "9cd6412e-e1e9-4b20-aa78-a9ba03ea240d",
    "florencia celsi": "318bdbf8-04dc-4953-b284-d3c5f429cbbf",
    "fernando rojas": "3b90bf47-16be-4116-b348-fd1bf2b9ef8c",
}

DURACION_SLOTS = {
    "b5af188f-aa9e-4983-8365-92930cbc9eeb": 45,
    "9cd6412e-e1e9-4b20-aa78-a9ba03ea240d": 30,
    "318bdbf8-04dc-4953-b284-d3c5f429cbbf": 30,
    "3b90bf47-16be-4116-b348-fd1bf2b9ef8c": 30,
}

MESES = {
    "ene": "01", "feb": "02", "mar": "03", "abr": "04",
    "may": "05", "jun": "06", "jul": "07", "ago": "08",
    "sep": "09", "oct": "10", "nov": "11", "dic": "12",
    "enero": "01", "febrero": "02", "marzo": "03", "abril": "04",
    "mayo": "05", "junio": "06", "julio": "07", "agosto": "08",
    "septiembre": "09", "octubre": "10", "noviembre": "11", "diciembre": "12",
}

# BUG 1 FIX: trigger ampliado — Claude puede confirmar sin usar "agend"
PALABRAS_CONFIRMACION = [
    "agend", "reserv", "quedaste anotad", "quedó anotad",
    "turno confirmado", "te registr", "listo, tu turno",
    "quedó reservad", "quedo reservad",
]


def cargar_config_prompts() -> dict:
    try:
        with open("config/prompts.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def cargar_system_prompt() -> str:
    return cargar_config_prompts().get("system_prompt", "Eres un asistente util.")


def obtener_mensaje_error() -> str:
    return cargar_config_prompts().get("error_message", "Lo siento, estoy teniendo problemas tecnicos.")


def obtener_mensaje_fallback() -> str:
    return cargar_config_prompts().get("fallback_message", "Disculpa, no entendi tu mensaje.")


def detectar_profesional(texto: str) -> str | None:
    texto_lower = texto.lower()
    for nombre, pid in PROFESIONALES.items():
        apellido = nombre.split()[-1]
        if apellido in texto_lower or nombre in texto_lower:
            return pid
    return None


def detectar_especialidad(texto: str) -> str | None:
    texto_lower = texto.lower()
    if any(p in texto_lower for p in ["ortodoncia", "brackets", "aparatos"]):
        return "Ortodoncia"
    if any(p in texto_lower for p in ["cirugia", "cirug", "extraccion", "muela del juicio"]):
        return "Cirugia"
    if any(p in texto_lower for p in ["general", "limpieza", "caries", "blanqueamiento", "estetica", "implante"]):
        return "Odontologia General"
    return None


def extraer_dni(texto: str) -> str | None:
    """Extrae un DNI del texto (7-8 dígitos)."""
    match = re.search(r'(?<!\d)(\d{7,8})(?!\d)', texto)
    return match.group(1) if match else None


def extraer_datos_confirmacion(
    historial: list[dict],
    respuesta: str,
    telefono: str,
    mensaje_actual: str = "",   # BUG 4 FIX: recibe el mensaje del usuario actual
) -> dict | None:
    """
    Extrae todos los datos necesarios para registrar el turno
    cuando Claude confirma el agendamiento.
    """
    texto_respuesta = respuesta.lower()

    # BUG 1 FIX: trigger ampliado con múltiples palabras de confirmación
    es_confirmacion = any(p in texto_respuesta for p in PALABRAS_CONFIRMACION)
    logger.warning(f"[DIAGN�STICO] extraer_datos_confirmacion llamada. es_confirmacion={es_confirmacion}. Respuesta: {texto_respuesta[:60]}")
    if not es_confirmacion:
        logger.debug(f"No es confirmacion. Respuesta: {texto_respuesta[:80]}")
        return None

    # BUG 4 FIX: incluir mensaje_actual en el texto de búsqueda
    texto_conv = (
        " ".join([m.get("content", "") for m in historial])
        + " " + mensaje_actual
        + " " + respuesta
    )

    # Profesional
    profesional_id = detectar_profesional(texto_conv)
    if not profesional_id:
        logger.warning("No se detectó profesional en la conversación")
        return None

    # BUG 2 FIX: regex de hora más robusto, sin \b problemático
    match_hora = re.search(r'(\d{1,2}):(\d{2})\s*hs?', texto_respuesta)
    if not match_hora:
        # fallback sin "hs"
        match_hora = re.search(r'(\d{1,2}):(\d{2})', texto_respuesta)
    if not match_hora:
        logger.warning("No se detectó hora en la respuesta de confirmación")
        return None
    hora = f"{int(match_hora.group(1)):02d}:{match_hora.group(2)}"

    # BUG 3 FIX: buscar fecha DD/MM excluyendo lo que ya matcheó como hora
    fecha = None

    # Primero intentar "DD de mes" (más preciso, no confunde con hora)
    match_fecha2 = re.search(r'\b(\d{1,2})\s+de\s+(\w+)\b', texto_respuesta)
    if match_fecha2:
        dia = int(match_fecha2.group(1))
        mes_num = MESES.get(match_fecha2.group(2).lower())
        if mes_num:
            fecha = f"2026-{mes_num}-{dia:02d}"

    # Luego intentar DD/MM asegurándonos que el mes sea válido (1-12)
    if not fecha:
        for m in re.finditer(r'(\d{1,2})/(\d{1,2})', texto_respuesta):
            dia_c = int(m.group(1))
            mes_c = int(m.group(2))
            if 1 <= dia_c <= 31 and 1 <= mes_c <= 12:
                fecha = f"2026-{mes_c:02d}-{dia_c:02d}"
                break

    if not fecha:
        logger.warning("No se detectó fecha en la respuesta de confirmación")
        return None

    # BUG 4 FIX: buscar DNI incluyendo el mensaje_actual
    dni = None
    textos_dni = [mensaje_actual] + [m.get("content", "") for m in historial]
    for texto in textos_dni:
        dni = extraer_dni(texto)
        if dni:
            break

    if not dni:
        logger.warning("No se encontró DNI en la conversación completa")

    # Nombre y apellido
    nombre = ""
    apellido = ""
    for msg in historial:
        if msg.get("role") == "user":
            contenido = msg.get("content", "").strip()
            partes = contenido.split()
            if 1 <= len(partes) <= 4 and not any(
                p in contenido.lower() for p in [
                    "turno", "quiero", "hola", "necesito", "si", "no",
                    "galeno", "osde", "swiss", "sancor", "ospe", "osecac",
                    "ortodoncia", "cirugia", "limpieza", "caries"
                ]
            ) and not extraer_dni(contenido):
                nombre = partes[0]
                apellido = " ".join(partes[1:]) if len(partes) > 1 else ""
                break

    # Obra social — BUG 5 FIX: normalizar a title case para Supabase
    OBRAS_MAP = {
        "osde": "OSDE",
        "osecac": "OSECAC",
        "ospe": "OSPE",
        "swiss medical": "Swiss Medical",
        "galeno": "Galeno",
        "sancor salud": "Sancor Salud",
    }
    obra_social = None
    for clave, nombre_normalizado in OBRAS_MAP.items():
        if clave in texto_conv.lower():
            obra_social = nombre_normalizado
            break

    # Motivo
    motivo = "Consulta odontologica"
    for msg in historial:
        if msg.get("role") == "user":
            contenido = msg.get("content", "").lower()
            if any(p in contenido for p in ["ortodoncia", "limpieza", "caries", "blanqueamiento", "cirugia", "implante", "extraccion"]):
                motivo = msg["content"]
                break

    datos = {
        "profesional_id": profesional_id,
        "fecha": fecha,
        "hora_inicio": hora,
        "duracion_min": DURACION_SLOTS.get(profesional_id, 30),
        "nombre": nombre or "Paciente",
        "apellido": apellido,
        "telefono": telefono,
        "motivo": motivo,
        "dni": dni or "",
        "obra_social": obra_social,
    }

    logger.info(f"[extraer_datos_confirmacion] datos={datos}")
    return datos


async def construir_contexto_supabase(mensaje: str, historial: list[dict]) -> str:
    contexto_parts = []

    texto_completo = mensaje.lower()
    for msg in historial[-6:]:
        texto_completo += " " + msg.get("content", "").lower()

    profesional_id = detectar_profesional(mensaje.lower()) or detectar_profesional(texto_completo)
    especialidad = detectar_especialidad(texto_completo)
    palabras_disponibilidad = ["disponib", "fecha", "dia", "horario", "turno", "cuando", "sabado", "lunes", "martes", "miercoles", "jueves", "viernes", "quiero", "sacar", "agendar"]
    pregunta_disponibilidad = any(p in texto_completo for p in palabras_disponibilidad)

    if profesional_id:
        fechas = await obtener_proximas_fechas_disponibles(profesional_id)
        if fechas:
            lineas = [f"  * {f['dia_nombre']} {f['fecha']}: {', '.join(f['slots'])}" for f in fechas]
            contexto_parts.append(
                "DISPONIBILIDAD REAL:\n" + "\n".join(lineas) +
                "\nUSA EXACTAMENTE estos datos. NO inventes horarios."
            )
        else:
            contexto_parts.append("DISPONIBILIDAD REAL: No hay turnos disponibles en los proximos 14 dias.")

    elif especialidad and pregunta_disponibilidad:
        profesionales = await obtener_profesionales_por_especialidad(especialidad)
        for prof in profesionales:
            fechas = await obtener_proximas_fechas_disponibles(prof["id"])
            if fechas:
                lineas = [f"    * {f['dia_nombre']} {f['fecha']}: {', '.join(f['slots'])}" for f in fechas]
                contexto_parts.append(
                    f"DISPONIBILIDAD REAL de {prof['nombre']} {prof['apellido']}:\n" + "\n".join(lineas)
                )

    if any(p in texto_completo for p in ["obra social", "osde", "swiss", "galeno", "sancor", "osecac", "ospe"]):
        obras = await obtener_obras_sociales()
        if obras:
            contexto_parts.append("OBRAS SOCIALES ACEPTADAS: " + ", ".join(obras))

    contexto_parts.append(
        "INSTRUCCION REGISTRO: Cuando confirmes el turno incluí 'te agendé' y "
        "la fecha en formato DD/MM y la hora en formato HH:MM."
    )

    if contexto_parts:
        return "\n\n---\nINFO EN TIEMPO REAL:\n" + "\n\n".join(contexto_parts) + "\n---"
    return ""


async def generar_respuesta(mensaje: str, historial: list[dict], telefono: str = "") -> str:
    if not mensaje or len(mensaje.strip()) < 2:
        return obtener_mensaje_fallback()

    system_prompt = cargar_system_prompt()

    try:
        contexto_real = await construir_contexto_supabase(mensaje, historial)
        if contexto_real:
            system_prompt += contexto_real
            logger.info("Contexto de Supabase agregado al prompt")
    except Exception as e:
        logger.error(f"Error consultando Supabase: {e}")

    mensajes = [{"role": m["role"], "content": m["content"]} for m in historial]
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

        # Registrar turno si es una confirmacion
        if telefono:
            try:
                # BUG 4 FIX: pasar mensaje_actual para que el DNI del último mensaje sea visible
                datos = extraer_datos_confirmacion(historial, respuesta, telefono, mensaje_actual=mensaje)
                if datos:
                    if not datos.get("dni"):
                        logger.warning("Confirmacion sin DNI — no se registra el turno")
                    else:
                        resultado = await registrar_turno_supabase(**datos)
                        if resultado.get("ok"):
                            logger.info(f"Turno registrado: {resultado.get('id')}")
                        else:
                            logger.warning(f"Error registrando turno: {resultado}")
            except Exception as e:
                logger.error(f"Error en registro de turno: {e}")

        return respuesta
    except Exception as e:
        logger.error(f"Error Claude API: {e}")
        return obtener_mensaje_error()
