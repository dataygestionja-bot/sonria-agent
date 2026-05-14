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
    buscar_paciente_por_dni,
    buscar_paciente_por_telefono,
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

PALABRAS_CONFIRMACION = [
    "agend", "reserv", "quedaste anotad", "quedo anotad",
    "turno confirmado", "te registr", "listo, tu turno",
    "quedo reservad",
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
    match = re.search(r'(?<!\d)(\d{7,8})(?!\d)', texto)
    return match.group(1) if match else None


def extraer_datos_confirmacion(
    historial: list[dict],
    respuesta: str,
    telefono: str,
    mensaje_actual: str = "",
) -> dict | None:
    texto_respuesta = respuesta.lower()

    es_confirmacion = any(p in texto_respuesta for p in PALABRAS_CONFIRMACION)
    logger.warning("[DIAG] extraer_datos_confirmacion llamada. confirmacion=" + str(es_confirmacion) + ". resp=" + texto_respuesta[:80])

    if not es_confirmacion:
        return None

    texto_conv = (
        " ".join([m.get("content", "") for m in historial])
        + " " + mensaje_actual
        + " " + respuesta
    )

    profesional_id = detectar_profesional(texto_conv)
    if not profesional_id:
        logger.warning("[DIAG] No se detecto profesional")
        return None

    match_hora = re.search(r'(\d{1,2}):(\d{2})\s*hs?', texto_respuesta)
    if not match_hora:
        match_hora = re.search(r'(\d{1,2}):(\d{2})', texto_respuesta)
    if not match_hora:
        logger.warning("[DIAG] No se detecto hora")
        return None
    hora = f"{int(match_hora.group(1)):02d}:{match_hora.group(2)}"

    fecha = None
    match_fecha2 = re.search(r'\b(\d{1,2})\s+de\s+(\w+)\b', texto_respuesta)
    if match_fecha2:
        dia = int(match_fecha2.group(1))
        mes_num = MESES.get(match_fecha2.group(2).lower())
        if mes_num:
            fecha = f"2026-{mes_num}-{dia:02d}"

    if not fecha:
        for m in re.finditer(r'(\d{1,2})/(\d{1,2})', texto_respuesta):
            dia_c = int(m.group(1))
            mes_c = int(m.group(2))
            if 1 <= dia_c <= 31 and 1 <= mes_c <= 12:
                fecha = f"2026-{mes_c:02d}-{dia_c:02d}"
                break

    if not fecha:
        logger.warning("[DIAG] No se detecto fecha")
        return None

    dni = None
    textos_dni = [mensaje_actual] + [m.get("content", "") for m in historial]
    for texto in textos_dni:
        dni = extraer_dni(texto)
        if dni:
            break

    if not dni:
        logger.warning("[DIAG] No se encontro DNI")

    # Nombre y apellido — buscar en historial
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

    # Nombre desde contexto del paciente registrado (buscar en respuestas del asistente)
    if not nombre:
        for msg in historial:
            if msg.get("role") == "assistant":
                contenido = msg.get("content", "")
                m = re.search(r"Nombre:\s*([^\n•]+)", contenido)
                if m:
                    partes = m.group(1).strip().split()
                    if partes:
                        nombre = partes[0]
                        apellido = " ".join(partes[1:]) if len(partes) > 1 else ""
                    break

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

    logger.warning("[DIAG] datos extraidos=" + str(datos))
    return datos


def detectar_actualizacion_dato(historial: list[dict], respuesta: str, paciente_id: str | None = None) -> dict | None:
    texto = respuesta.lower()

    PALABRAS_ACTUALIZACION = [
        "quedarían así",
        "quedarian asi",
        "actualicé tu obra social",
        "actualice tu obra social",
    ]
    if not any(p in texto for p in PALABRAS_ACTUALIZACION):
        return None

    if not paciente_id:
        return None

    datos_actualizar = {}

    OBRAS_MAP = {
        "osde": "OSDE",
        "osecac": "OSECAC",
        "ospe": "OSPE",
        "swiss medical": "Swiss Medical",
        "galeno": "Galeno",
        "sancor salud": "Sancor Salud",
        "particular": None,
    }
    for clave, valor in OBRAS_MAP.items():
        if clave in texto:
            if valor:
                datos_actualizar["obra_social_nombre"] = valor
            else:
                datos_actualizar["obra_social_id"] = None
            break

    match_tel = re.search(r'\b(549\d{10}|\d{10,13})\b', texto)
    if match_tel:
        datos_actualizar["telefono"] = match_tel.group(1)

    if not datos_actualizar:
        return None

    return {"paciente_id": paciente_id, "datos": datos_actualizar}


async def construir_contexto_paciente(mensaje: str, historial: list[dict], telefono: str) -> tuple[str, str | None]:
    contexto = ""
    paciente_id = None

    dni = extraer_dni(mensaje)

    if not dni:
        for msg in historial:
            if msg.get("role") == "user":
                dni = extraer_dni(msg.get("content", ""))
                if dni:
                    break

    if dni:
        paciente = await buscar_paciente_por_dni(dni)
        if paciente:
            paciente_id = paciente.get("id")
            obra = paciente.get("obra_social_nombre") or "Particular"
            contexto = (
                f"\n\nPACIENTE ENCONTRADO EN BD:\n"
                f"- ID: {paciente.get('id')}\n"
                f"- Nombre: {paciente.get('nombre')} {paciente.get('apellido')}\n"
                f"- DNI: {paciente.get('dni')}\n"
                f"- Telefono: {paciente.get('telefono')}\n"
                f"- Obra social: {obra}\n"
                f"Mostrá estos datos al paciente y pedí confirmación."
            )
        else:
            contexto = (
                f"\n\nPACIENTE NO ENCONTRADO EN BD (DNI: {dni}).\n"
                f"Informá al paciente que no está registrado y pedí nombre y apellido para darlo de alta."
            )
    elif telefono and len(historial) == 0:
        paciente = await buscar_paciente_por_telefono(telefono)
        if paciente:
            paciente_id = paciente.get("id")
            obra = paciente.get("obra_social_nombre") or "Particular"
            contexto = (
                f"\n\nPACIENTE RECONOCIDO POR TELEFONO:\n"
                f"- Nombre: {paciente.get('nombre')} {paciente.get('apellido')}\n"
                f"- DNI: {paciente.get('dni')}\n"
                f"- Telefono: {paciente.get('telefono')}\n"
                f"- Obra social: {obra}\n"
                f"Saludá al paciente por su nombre. Igualmente pedí su DNI para confirmar identidad."
            )

    return contexto, paciente_id


async def construir_contexto_supabase(mensaje: str, historial: list[dict]) -> str:
    contexto_parts = []

    texto_completo = mensaje.lower()
    for msg in historial[-6:]:
        texto_completo += " " + msg.get("content", "").lower()

    profesional_id = detectar_profesional(mensaje.lower())
    if not profesional_id:
        for msg in reversed(historial[-4:]):
            if msg.get("role") == "assistant":
                pid = detectar_profesional(msg.get("content", "").lower())
                if pid:
                    profesional_id = pid
                    break
    especialidad = detectar_especialidad(texto_completo)
    palabras_disponibilidad = [
        "disponib", "fecha", "dia", "horario", "turno", "cuando",
        "sabado", "lunes", "martes", "miercoles", "jueves", "viernes",
        "quiero", "sacar", "agendar", "solicitar"
    ]
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
        "INSTRUCCION REGISTRO: Cuando confirmes el turno inclui 'te agende' y "
        "la fecha en formato DD/MM y la hora en formato HH:MM."
    )

    if contexto_parts:
        return "\n\n---\nINFO EN TIEMPO REAL:\n" + "\n\n".join(contexto_parts) + "\n---"
    return ""


async def generar_respuesta(mensaje: str, historial: list[dict], telefono: str = "") -> str:
    if not mensaje or len(mensaje.strip()) < 1:
        return obtener_mensaje_fallback()

    system_prompt = cargar_system_prompt()

    try:
        # Contexto del paciente (DNI / teléfono)
        contexto_paciente, paciente_id_actual = await construir_contexto_paciente(mensaje, historial, telefono)
        if contexto_paciente:
            system_prompt += contexto_paciente

        # Contexto de disponibilidad y obras sociales
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

        if telefono:
            try:
                datos = extraer_datos_confirmacion(historial, respuesta, telefono, mensaje_actual=mensaje)
                if datos:
                    if not datos.get("dni"):
                        logger.warning("[DIAG] Confirmacion sin DNI — no se registra el turno")
                    else:
                        resultado = await registrar_turno_supabase(**datos)
                        if resultado.get("ok"):
                            logger.warning("[DIAG] Turno registrado OK: " + str(resultado.get("id")))
                        else:
                            logger.warning("[DIAG] Error registrando turno: " + str(resultado))
            except Exception as e:
                logger.error(f"Error en registro de turno: {e}")

        # Detectar y ejecutar actualización de datos del paciente
        if telefono:
            try:
                actualizacion = detectar_actualizacion_dato(historial, respuesta, paciente_id=paciente_id_actual)
                if actualizacion:
                    from agent.tools import actualizar_paciente, buscar_obra_social_id
                    datos = actualizacion["datos"]
                    paciente_id = actualizacion["paciente_id"]
                    if "obra_social_nombre" in datos:
                        obra_id = await buscar_obra_social_id(datos.pop("obra_social_nombre"))
                        if obra_id:
                            datos["obra_social_id"] = obra_id
                    resultado = await actualizar_paciente(paciente_id, datos)
                    logger.warning("[DIAG] Paciente actualizado: " + str(resultado))
            except Exception as e:
                logger.error(f"Error actualizando paciente: {e}")

        return respuesta
    except Exception as e:
        logger.error(f"Error Claude API: {e}")
        return obtener_mensaje_error()
