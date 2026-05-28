# agent/brain.py — Cerebro del agente: conexión con Claude API
import os
import re
import asyncio
import yaml
import logging
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from agent.tools import (
    obtener_profesionales_por_especialidad,
    obtener_proximas_fechas_disponibles,
    registrar_turno_supabase,
    buscar_paciente_por_dni,
    buscar_paciente_por_telefono,
    crear_paciente,
    obtener_turnos_paciente,
    cancelar_turno,
    log_bot_event,
)
from agent.memory import limpiar_historial

load_dotenv()
logger = logging.getLogger("agentkit")

CONSULTORIO_TELEFONO = os.getenv("CONSULTORIO_TELEFONO", "el consultorio")

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


def detectar_profesional_de_historial(mensaje_actual: str, historial: list[dict]) -> str | None:
    """
    Detecta el profesional_id que el paciente seleccionó, escaneando
    todo el historial. Orden de confianza:
      1. Nombre en el mensaje actual
      2. Recorre historial completo hacia atrás: si el usuario escribió un
         nombre de profesional lo devuelve; si escribió un número busca
         el listado del asistente anterior y mapea la posición.
      3. Último recurso: mensaje del asistente que mencione exactamente
         un único profesional.
    """
    # 1. Mensaje actual
    pid = detectar_profesional(mensaje_actual.lower())
    if pid:
        return pid

    # 2. Recorre todo el historial de más reciente a más antiguo
    for i in range(len(historial) - 1, -1, -1):
        msg = historial[i]
        if msg.get("role") != "user":
            continue
        contenido_user = msg.get("content", "").strip()

        # ¿El usuario dijo el nombre del profesional?
        pid = detectar_profesional(contenido_user.lower())
        if pid:
            return pid

        # ¿El usuario respondió con un número simple?
        if contenido_user.isdigit():
            num = contenido_user
            # Buscar el asistente que lo precedió y tenía el listado numerado
            for j in range(i - 1, max(i - 6, -1), -1):
                if historial[j].get("role") == "assistant":
                    contenido_asist = historial[j].get("content", "")
                    match_linea = re.search(
                        rf'^{re.escape(num)}\.\s+(.+)',
                        contenido_asist,
                        re.MULTILINE
                    )
                    if match_linea:
                        pid = detectar_profesional(match_linea.group(1).lower())
                        if pid:
                            return pid
                    break  # solo el asistente inmediatamente anterior

    # 3. Último recurso: asistente reciente que menciona UN único profesional
    for msg in reversed(historial[-8:]):
        if msg.get("role") == "assistant":
            contenido = msg.get("content", "").lower()
            encontrados = [pid for nombre, pid in PROFESIONALES.items()
                           if nombre.split()[-1] in contenido]
            if len(encontrados) == 1:
                return encontrados[0]

    return None


def detectar_fecha_hora_de_historial(historial: list[dict]) -> tuple[str, str] | None:
    """
    Mapea la elección numérica del paciente (ej: "4") al slot correcto
    del listado que mostró el asistente anteriormente.

    Formato esperado en el mensaje del asistente (generado desde prompts.yaml 4.2):
      "1. Jueves 28/05 — 09:00hs"
      "4. Viernes 29/05 — 15:30hs"

    Retorna (fecha_ISO "2026-MM-DD", hora "HH:MM") o None si no puede mapear.
    """
    _PATRON_SLOT = re.compile(
        r'^(\d)\.\s+\S+\s+(\d{1,2})/(\d{1,2})\s*[—\-]+\s*(\d{1,2}):(\d{2})hs?',
        re.MULTILINE,
    )

    for i in range(len(historial) - 1, -1, -1):
        msg = historial[i]
        if msg.get("role") != "user":
            continue
        user_text = msg.get("content", "").strip()
        if not user_text.isdigit():
            continue
        num = int(user_text)

        # Buscar el asistente inmediatamente anterior con un listado de slots
        for j in range(i - 1, max(i - 5, -1), -1):
            if historial[j].get("role") != "assistant":
                continue
            asist_content = historial[j].get("content", "")
            if not _PATRON_SLOT.search(asist_content):
                break  # el asistente inmediatamente anterior no tiene slots
            for m in _PATRON_SLOT.finditer(asist_content):
                if int(m.group(1)) == num:
                    dia = int(m.group(2))
                    mes = int(m.group(3))
                    hora_h = int(m.group(4))
                    hora_m = m.group(5)
                    fecha_iso = f"2026-{mes:02d}-{dia:02d}"
                    hora_fmt = f"{hora_h:02d}:{hora_m}"
                    logger.info(
                        f"[FECHA-HORA] Mapeado del historial: "
                        f"usuario eligió {num} → {fecha_iso} {hora_fmt}"
                    )
                    return fecha_iso, hora_fmt
            break  # solo mirar el asistente inmediatamente anterior

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


_SECUENCIAS_DNI_INVALIDAS = {"123456", "1234567", "12345678"}


def _dni_valido(dni: str) -> bool:
    """
    Valida un DNI ya extraído (solo dígitos).
    Rechaza:
      - Primer dígito 0
      - Todos los dígitos iguales (111111, 2222222, etc.)
      - Secuencias obvias (123456, 1234567, 12345678)
    """
    if not dni:
        return False
    if dni[0] == "0":
        return False
    if len(set(dni)) == 1:
        return False
    if dni in _SECUENCIAS_DNI_INVALIDAS:
        return False
    return True


def extraer_dni(texto: str) -> str | None:
    # Primero: formato con puntos estilo argentino — 1.234.567 o 12.345.678
    match_puntos = re.search(r'(?<!\d)(\d{1,2}\.\d{3}\.\d{3})(?!\d)', texto)
    if match_puntos:
        candidato = match_puntos.group(1).replace(".", "")
        return candidato if _dni_valido(candidato) else None
    match_puntos6 = re.search(r'(?<!\d)(\d{1,3}\.\d{3})(?!\d)', texto)
    if match_puntos6:
        candidato = match_puntos6.group(1).replace(".", "")
        if 6 <= len(candidato) <= 8:
            return candidato if _dni_valido(candidato) else None
    # Segundo: 6-8 dígitos consecutivos (sin puntos)
    match = re.search(r'(?<!\d)(\d{6,8})(?!\d)', texto)
    if match:
        candidato = match.group(1)
        return candidato if _dni_valido(candidato) else None
    return None


def extraer_datos_confirmacion(
    historial: list[dict],
    respuesta: str,
    telefono: str,
    mensaje_actual: str = "",
) -> dict | None:
    texto_respuesta = respuesta.lower()

    es_confirmacion = any(p in texto_respuesta for p in PALABRAS_CONFIRMACION)
    logger.warning("[DIAG] extraer_datos_confirmacion llamada. confirmacion=" + str(es_confirmacion) + ". resp=" + texto_respuesta[:80])

    # Si la respuesta actual no es una confirmación (ej: "¡Genial! Nos vemos en el
    # consultorio"), buscar en los últimos mensajes del asistente del historial.
    # Cubre el caso en que Claude confirmó en el mensaje anterior y ahora despide.
    if not es_confirmacion:
        for msg in reversed(historial[-8:]):
            if msg.get("role") != "assistant":
                continue
            contenido_hist = msg.get("content", "")
            if any(p in contenido_hist.lower() for p in PALABRAS_CONFIRMACION):
                logger.warning("[DIAG] Confirmacion encontrada en historial reciente — usando ese mensaje para extracción")
                respuesta = contenido_hist
                texto_respuesta = respuesta.lower()
                es_confirmacion = True
                break

    if not es_confirmacion:
        return None

    texto_conv = (
        " ".join([m.get("content", "") for m in historial])
        + " " + mensaje_actual
        + " " + respuesta
    )

    # Buscar profesional en la respuesta de confirmación primero (más confiable:
    # Claude siempre nombra al profesional en el mensaje de confirmación del turno)
    profesional_id = detectar_profesional(respuesta)
    if not profesional_id:
        # Fallback: escanear historial completo
        profesional_id = detectar_profesional_de_historial(mensaje_actual, historial)
    if not profesional_id:
        logger.warning("[DIAG] No se detecto profesional")
        return None

    # 1. Mapear la elección numérica del paciente al slot correcto del historial
    resultado_fh = detectar_fecha_hora_de_historial(historial)
    if resultado_fh:
        fecha, hora = resultado_fh
        logger.info(f"[DIAG] Fecha/hora obtenida del historial: {fecha} {hora}")
    else:
        # 2. Fallback: extraer de la respuesta de confirmación de Claude
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
    # Primero: respuesta directa a la pregunta de nombre/apellido de Claude
    for i, msg in enumerate(historial):
        if msg.get("role") == "assistant":
            contenido_asistente = msg.get("content", "").lower()
            if "nombre y apellido" in contenido_asistente or "me decís tu nombre" in contenido_asistente or "me decis tu nombre" in contenido_asistente:
                for j in range(i + 1, len(historial)):
                    if historial[j].get("role") == "user":
                        respuesta_nombre = historial[j].get("content", "").strip()
                        partes_n = respuesta_nombre.split()
                        if 1 <= len(partes_n) <= 4 and not extraer_dni(respuesta_nombre) and not respuesta_nombre.isdigit():
                            nombre = partes_n[0].capitalize()
                            apellido = " ".join(p.capitalize() for p in partes_n[1:]) if len(partes_n) > 1 else ""
                        break
                break
    # Fallback: heurística genérica
    if not nombre:
        for msg in historial:
            if msg.get("role") == "user":
                contenido = msg.get("content", "").strip()
                partes = contenido.split()
                contenido_lower = contenido.lower()
                if 1 <= len(partes) <= 4 and not any(
                    p in contenido_lower for p in [
                        "turno", "quiero", "hola", "necesito",
                        "galeno", "osde", "swiss", "sancor", "ospe", "osecac",
                        "ortodoncia", "cirugia", "limpieza", "caries"
                    ]
                ) and not re.search(r'\b(si|sí|no)\b', contenido_lower) \
                  and not extraer_dni(contenido) and not contenido.strip().isdigit():
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

    motivo = "Consulta odontologica"
    for msg in historial:
        if msg.get("role") == "user":
            contenido = msg.get("content", "").lower()
            if any(p in contenido for p in ["ortodoncia", "limpieza", "caries", "blanqueamiento", "cirugia", "implante", "extraccion"]):
                motivo = msg["content"]
                break

    # Detectar si el turno es para un tercero y capturar su teléfono
    # El bot pregunta "Ingresá el número del paciente" → usuario responde con el número
    telefono_turno = telefono  # default: teléfono del remitente
    _frases_tel_tercero = [
        "ingresá el número del paciente",
        "ingresa el número del paciente",
        "número del paciente con formato",
        "número de contacto del paciente",
    ]
    for i, msg in enumerate(historial):
        if msg.get("role") == "assistant":
            contenido_asist = msg.get("content", "").lower()
            if any(f in contenido_asist for f in _frases_tel_tercero):
                # La respuesta inmediata del usuario es el número del tercero
                for j in range(i + 1, len(historial)):
                    if historial[j].get("role") == "user":
                        tel_candidato = re.sub(r'\D', '', historial[j].get("content", ""))
                        if 11 <= len(tel_candidato) <= 13:
                            telefono_turno = tel_candidato
                            logger.info(f"[TERCERO] Teléfono del tercero detectado: {telefono_turno}")
                        break
                if telefono_turno != telefono:
                    break

    datos = {
        "profesional_id": profesional_id,
        "fecha": fecha,
        "hora_inicio": hora,
        "duracion_min": DURACION_SLOTS.get(profesional_id, 30),
        "nombre": nombre or "",
        "apellido": apellido,
        "telefono": telefono_turno,
        "motivo": motivo,
        "dni": dni or "",
    }

    logger.warning("[DIAG] datos extraidos=" + str(datos))
    return datos


def detectar_actualizacion_dato(historial: list[dict], respuesta: str, paciente_id: str | None = None) -> dict | None:
    texto = respuesta.lower()

    if not paciente_id:
        return None

    PALABRAS_ACTUALIZACION = [
        "actualicé", "actualice", "actualicé tu",
        "cambié", "cambie",
        "quedarian asi", "quedarían así",
    ]
    if not any(p in texto for p in PALABRAS_ACTUALIZACION):
        return None

    if any(p in texto for p in ["agend", "reserv", "turno confirmado"]):
        return None

    datos_actualizar = {}

    match_tel = re.search(r'\b(549\d{10}|\d{10,13})\b', texto)
    if match_tel:
        datos_actualizar["telefono"] = match_tel.group(1)

    match_email = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', texto)
    if match_email:
        datos_actualizar["email"] = match_email.group(0)

    if not datos_actualizar:
        return None

    return {"paciente_id": paciente_id, "datos": datos_actualizar}


def _es_solicitud_nombre(msg_asistente: str) -> bool:
    """Retorna True si el mensaje del asistente está pidiendo nombre y apellido."""
    lower = msg_asistente.lower()
    return any(p in lower for p in [
        "nombre y apellido",
        "me decís tu nombre",
        "me decis tu nombre",
        "decime tu nombre",
        "cómo se escribe tu nombre",
        "como se escribe tu nombre",
        "escribí tu nombre",
        "escribe tu nombre",
        "tu nombre completo",
        "nombre completo",
        "no parece ser un nombre válido",
        "no parece ser un nombre valido",
        "escribir tu nombre",
    ])


def _limpiar_nombre(texto: str) -> str:
    """
    Elimina caracteres que no corresponden a un nombre de persona.
    Conserva: letras (con tildes/ñ), espacios, guiones, apóstrofes.
    """
    limpio = re.sub(r"[^a-záéíóúüñàèìòùâêîôûãõäëïöüÁÉÍÓÚÜÑ'\-\s]", "", texto, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", limpio).strip()


# Palabras que el paciente puede escribir en lugar de su nombre
_PALABRAS_INVALIDAS_NOMBRE = {
    "chau", "hola", "si", "sí", "no", "ok", "gracias", "buenas",
    "bye", "adios", "adiós", "nada", "dale", "listo", "perfecto",
    "claro", "bueno", "bien", "mal", "quizas", "quizás", "depende",
    "entendido", "ninguno", "ninguna",
}


def _es_nombre_valido(texto: str) -> bool:
    """
    Retorna True si el texto puede ser un nombre de persona.
    Retorna False (→ pedir de nuevo) si:
      - Menos de 3 caracteres
      - Solo dígitos o es un DNI
      - El texto completo (o todas sus palabras) está en la lista negra
      - No contiene ninguna vocal
    """
    strip = texto.strip()
    lower = strip.lower()

    if len(strip) < 3:
        return False

    if strip.isdigit() or extraer_dni(strip):
        return False

    # Texto completo en lista negra
    if lower in _PALABRAS_INVALIDAS_NOMBRE:
        return False

    # Todas las palabras en lista negra (ej: "hola chau")
    partes = lower.split()
    if partes and all(p in _PALABRAS_INVALIDAS_NOMBRE for p in partes):
        return False

    # Rechazar si contiene caracteres que no corresponden a un nombre
    # (símbolos, números, &, #, @, etc.)
    if re.search(r"[^a-zA-ZáéíóúüñÁÉÍÓÚÜÑàèìòùâêîôûãõäëïöü'\-\s]", strip):
        return False

    # Debe tener al menos una vocal
    if not re.search(r'[aeiouáéíóúüàèìòù]', lower):
        return False

    return True


def _extraer_nombre_apellido(texto: str) -> tuple[str, str] | None:
    """
    Intenta extraer nombre y apellido de un texto corto.
    Aplica _limpiar_nombre() SIEMPRE antes de guardar para eliminar
    caracteres especiales independientemente de si pasó la validación.
    Retorna (nombre, apellido) o None si no parece un nombre válido.
    """
    partes = texto.strip().split()
    if not (1 <= len(partes) <= 4):
        return None
    if not _es_nombre_valido(texto):
        return None
    # Descartar palabras de contexto odontológico
    texto_lower = texto.lower()
    palabras_contexto = [
        "turno", "quiero", "necesito",
        "galeno", "osde", "swiss", "sancor", "ospe", "osecac",
        "ortodoncia", "cirugia", "limpieza", "caries", "particular",
        "cancelar", "reprogramar", "consultar",
    ]
    if any(p in texto_lower for p in palabras_contexto):
        return None
    # Limpiar caracteres especiales SIEMPRE antes de guardar
    texto_limpio = _limpiar_nombre(texto)
    partes_limpias = texto_limpio.split()
    if not partes_limpias:
        return None
    nombre = partes_limpias[0].capitalize()
    apellido = " ".join(p.capitalize() for p in partes_limpias[1:]) if len(partes_limpias) > 1 else ""
    return nombre, apellido


async def registrar_paciente_si_es_nombre(
    mensaje: str,
    historial: list[dict],
    telefono: str,
    paciente_id_actual: str | None,
) -> str | None:
    """
    Registra inmediatamente al paciente en la DB cuando detecta que:
      - El paciente no está registrado aún (paciente_id_actual is None)
      - Hay un DNI en el historial que fue buscado y no encontrado
      - El último mensaje del asistente pedía nombre y apellido
      - El mensaje actual parece ser la respuesta con el nombre

    Retorna el nuevo paciente_id si lo creó, o None si no aplica.
    """
    if paciente_id_actual:
        return None  # Ya existe, nada que hacer

    # Verificar que el asistente anterior pidió el nombre
    ultimo_asistente = ""
    for msg in reversed(historial):
        if msg.get("role") == "assistant":
            ultimo_asistente = msg.get("content", "")
            break
    if not _es_solicitud_nombre(ultimo_asistente):
        return None

    # Obtener DNI del historial
    dni = None
    for msg in historial:
        if msg.get("role") == "user":
            dni = extraer_dni(msg.get("content", ""))
            if dni:
                break
    if not dni:
        return None

    # Verificar que el DNI efectivamente no está en BD (no crear duplicados)
    existente = await buscar_paciente_por_dni(dni)
    if existente:
        return existente.get("id")

    # Parsear nombre del mensaje actual
    resultado = _extraer_nombre_apellido(mensaje)
    if not resultado:
        return None
    nombre, apellido = resultado

    # Crear paciente con los datos disponibles hasta ahora
    nuevo = await crear_paciente(nombre, apellido, dni, telefono)
    nuevo_id = nuevo.get("id")
    if nuevo_id:
        logger.warning(f"[REGISTRO TEMPRANO] Paciente creado: id={nuevo_id} nombre={nombre} apellido={apellido} dni={dni}")
    return nuevo_id


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
            contexto = (
                f"\n\nPACIENTE ENCONTRADO EN BD:\n"
                f"- ID: {paciente.get('id')}\n"
                f"- Nombre: {paciente.get('nombre')} {paciente.get('apellido')}\n"
                f"- DNI: {paciente.get('dni')}\n"
                f"- Telefono: {paciente.get('telefono')}\n"
                f"Mostrá estos datos al paciente y pedí confirmación.\n"
                f"REGLA TURNO PARA PACIENTE EXISTENTE: Si el paciente solicita un turno, "
                f"NUNCA preguntes por especialidad. "
                f"Mostrá directamente la lista completa de profesionales (paso 4.1b):\n"
                f"1. Bruno Ordoñez — Odontología General\n"
                f"2. Fernando Rojas — Ortodoncia\n"
                f"3. Florencia Celsi — Cirugía\n"
                f"4. Federico Cabrera — Cirugía"
            )
        else:
            contexto = (
                f"\n\nPACIENTE NO ENCONTRADO EN BD (DNI: {dni}).\n"
                f"Informá al paciente que no está registrado y pedí nombre y apellido para darlo de alta."
            )
    return contexto, paciente_id


async def construir_contexto_turnos(paciente_id: str) -> str:
    from agent.tools import obtener_turnos_paciente
    turnos = await obtener_turnos_paciente(paciente_id)
    if not turnos:
        return "\n\nTURNOS DEL PACIENTE: No tiene turnos futuros confirmados."
    lineas = []
    for t in turnos:
        fecha = t.get("fecha", "")
        hora = t.get("hora_inicio", "")[:5]
        prof = t.get("profesional", "desconocido")
        turno_id = t.get("id", "")
        lineas.append(f"  - ID: {turno_id} | {fecha} {hora}hs con {prof}")
    return "\n\nTURNOS DEL PACIENTE:\n" + "\n".join(lineas)


def detectar_cancelacion_turno(respuesta: str) -> dict | None:
    """Detecta si Claude confirmó una cancelación y extrae el turno_id."""
    texto = respuesta.lower()
    if "cancelé" not in texto:
        return None
    match = re.search(
        r'\[id:?\s*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\]',
        texto
    )
    if not match:
        return None
    return {"turno_id": match.group(1)}


async def construir_contexto_supabase(mensaje: str, historial: list[dict]) -> str:
    contexto_parts = []

    texto_completo = mensaje.lower()
    for msg in historial[-6:]:
        texto_completo += " " + msg.get("content", "").lower()

    profesional_id = detectar_profesional_de_historial(mensaje, historial)
    logger.warning(f"[DIAG] construir_contexto_supabase — profesional_id={profesional_id}")
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

    # Inyectar turnos del paciente si pide cancelar, consultar o reprogramar
    paciente_id_ctx = None
    dni_ctx = extraer_dni(mensaje)
    if not dni_ctx:
        for msg in historial:
            if msg.get("role") == "user":
                dni_ctx = extraer_dni(msg.get("content", ""))
                if dni_ctx:
                    break
    if dni_ctx:
        p = await buscar_paciente_por_dni(dni_ctx)
        if p:
            paciente_id_ctx = p.get("id")

    # Detectar si el mensaje anterior fue el menú principal
    ultimo_asistente = ""
    for msg in reversed(historial):
        if msg.get("role") == "assistant":
            ultimo_asistente = msg.get("content", "").lower()
            break

    es_seleccion_menu = (
        "solicitar turno" in ultimo_asistente and
        "cancelar turno" in ultimo_asistente and
        mensaje.strip() in ["2", "3", "4"]
    )

    palabras_turnos = ["cancelar", "mis turnos", "turnos reservados", "reprogramar", "consultar turno"]
    if paciente_id_ctx and (any(p in texto_completo for p in palabras_turnos) or es_seleccion_menu):
        turnos = await obtener_turnos_paciente(paciente_id_ctx)
        if turnos:
            lineas = [
                f"  - ID: {t['id']} | {t['fecha']} {t['hora_inicio'][:5]}hs | "
                f"Dr/a {t.get('profesional', '?')} | {t.get('motivo_consulta', '')}"
                for t in turnos
            ]
            contexto_parts.append(
                "TURNOS DEL PACIENTE (usar IDs internamente, NO mostrarlos al paciente):\n" +
                "\n".join(lineas) +
                "\nCuando canceles un turno, incluí el ID así al FINAL del mensaje, sin línea nueva previa: [ID:uuid]"
            )
        else:
            contexto_parts.append("TURNOS DEL PACIENTE: No tiene turnos futuros confirmados.")

    contexto_parts.append(
        "INSTRUCCION REGISTRO: Cuando confirmes el turno inclui 'te agende' y "
        "la fecha en formato DD/MM y la hora en formato HH:MM."
    )

    if contexto_parts:
        return "\n\n---\nINFO EN TIEMPO REAL:\n" + "\n\n".join(contexto_parts) + "\n---"
    return ""


async def _retry_turno_background(datos: dict, telefono: str, proveedor) -> None:
    """
    Reintenta registrar un turno en background con backoff exponencial.
    Envía mensaje al paciente con el resultado final.
    Delays: intento 1 → 10s, intento 2 → 30s, intento 3 → 2min.
    """
    delays = [10, 30, 120]
    for i, delay in enumerate(delays):
        await asyncio.sleep(delay)
        try:
            resultado = await asyncio.wait_for(
                registrar_turno_supabase(**datos),
                timeout=10.0,
            )
            if resultado.get("ok"):
                logger.info(f"[TURNO-RETRY] Reintento {i + 1} exitoso para {telefono} — id={resultado.get('id')}")
                await proveedor.enviar_mensaje(
                    telefono,
                    "✅ ¡Tu reserva quedó registrada! Te esperamos en el consultorio 😊"
                )
                return
            else:
                logger.error(f"[TURNO-RETRY] Reintento {i + 1} fallido (respuesta negativa): {resultado}")
        except asyncio.TimeoutError:
            logger.error(f"[TURNO-RETRY] Reintento {i + 1} — timeout (10s)")
        except Exception as e:
            logger.error(f"[TURNO-RETRY] Reintento {i + 1} — excepción: {e}")

    # Todos los reintentos fallaron
    paciente_id = datos.get("paciente_id", "") or ""
    fecha = datos.get("fecha", "?")
    hora = datos.get("hora_inicio", "?")
    prof_id = datos.get("profesional_id", "?")
    detalle_fallo = f"{fecha} {hora} con profesional {prof_id} — fallo tras 3 reintentos"
    logger.critical(
        f"[ALERTA] Fallo al crear turno para paciente {paciente_id} — "
        f"{detalle_fallo}. Requiere intervención manual."
    )
    asyncio.create_task(log_bot_event(
        tipo="turno_fallido",
        nivel="critical",
        telefono=telefono,
        paciente_id=paciente_id,
        detalle=detalle_fallo,
    ))
    asyncio.create_task(log_bot_event(
        tipo="alerta",
        nivel="critical",
        telefono=telefono,
        paciente_id=paciente_id,
        detalle=f"Fallo definitivo al crear turno: {detalle_fallo}. Requiere intervención manual.",
    ))
    await proveedor.enviar_mensaje(
        telefono,
        f"Tuvimos un problema al registrar tu turno 😔 "
        f"Por favor contactá al consultorio directamente al {CONSULTORIO_TELEFONO}."
    )


async def generar_respuesta(mensaje: str, historial: list[dict], telefono: str = "", proveedor=None) -> str:
    if not mensaje or len(mensaje.strip()) < 1:
        return obtener_mensaje_fallback()

    system_prompt = cargar_system_prompt()

    try:
        # Contexto del paciente (DNI / teléfono)
        contexto_paciente, paciente_id_actual = await construir_contexto_paciente(mensaje, historial, telefono)
        if contexto_paciente:
            system_prompt += contexto_paciente

        # Registro temprano: si el mensaje actual es el nombre/apellido de un paciente
        # nuevo, lo insertamos en DB AHORA antes de continuar el flujo
        if telefono and not paciente_id_actual:
            # Detectar si el asistente acababa de pedir nombre y el texto no es válido
            ultimo_asistente = next(
                (m.get("content", "") for m in reversed(historial) if m.get("role") == "assistant"),
                ""
            )
            if _es_solicitud_nombre(ultimo_asistente) and not _es_nombre_valido(mensaje):
                logger.warning(f"[NOMBRE] Texto rechazado como nombre inválido: '{mensaje}'")
                return (
                    "Ese no parece ser un nombre válido 😊 "
                    "¿Me decís tu nombre y apellido completo? "
                    "Por ejemplo: Juan García"
                )

            try:
                nuevo_id = await registrar_paciente_si_es_nombre(
                    mensaje, historial, telefono, paciente_id_actual
                )
                if nuevo_id:
                    paciente_id_actual = nuevo_id
            except Exception as e:
                logger.error(f"Error en registro temprano de paciente: {e}")

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
                # Si el asistente estaba pidiendo corrección de nombre, no intentar
                # registrar un turno — el mensaje del paciente es un nombre, no confirmación
                _ultimo_asist_conf = next(
                    (m.get("content", "") for m in reversed(historial) if m.get("role") == "assistant"), ""
                )
                _en_flujo_nombre = _es_solicitud_nombre(_ultimo_asist_conf)
                datos = None if _en_flujo_nombre else extraer_datos_confirmacion(
                    historial, respuesta, telefono, mensaje_actual=mensaje
                )
                if datos:
                    if not datos.get("dni"):
                        logger.warning("[DIAG] Confirmacion sin DNI — no se registra el turno")
                    else:
                        # Primer intento con timeout de 10 segundos
                        primer_intento_ok = False
                        try:
                            resultado = await asyncio.wait_for(
                                registrar_turno_supabase(**datos),
                                timeout=10.0,
                            )
                            if resultado.get("ok"):
                                primer_intento_ok = True
                                logger.warning("[DIAG] Turno registrado OK: " + str(resultado.get("id")))
                                # Limpiar historial solo cuando el INSERT fue exitoso
                                try:
                                    await limpiar_historial(telefono)
                                    logger.info(f"[SESSION] Historial limpiado tras turno confirmado para {telefono}")
                                except Exception as e_limpiar:
                                    logger.error(f"[SESSION] Error limpiando historial tras turno: {e_limpiar}")
                            else:
                                logger.warning("[DIAG] Error registrando turno: " + str(resultado))
                        except asyncio.TimeoutError:
                            logger.error("[TURNO] Primer intento — timeout (10s)")
                        except Exception as e:
                            logger.error(f"[TURNO] Primer intento — excepción: {e}")

                        if not primer_intento_ok:
                            # Informar al paciente y lanzar reintentos en background
                            respuesta = (
                                "Estamos validando tu reserva 😊 "
                                "Te confirmaremos en unos instantes."
                            )
                            if proveedor:
                                asyncio.create_task(
                                    _retry_turno_background(datos, telefono, proveedor)
                                )
            except Exception as e:
                logger.error(f"Error en registro de turno: {e}")

        # Detectar y ejecutar cancelación de turno
        if telefono:
            try:
                cancelacion = detectar_cancelacion_turno(respuesta)
                if cancelacion:
                    resultado = await cancelar_turno(cancelacion["turno_id"])
                    logger.warning(f"[DIAG] Turno cancelado: {cancelacion['turno_id']} → {resultado}")
                    # Limpiar el UUID del mensaje antes de enviarlo al paciente
                    respuesta = re.sub(
                        r'\s*\[id:?\s*[0-9a-f\-]{36}\]',
                        '',
                        respuesta,
                        flags=re.IGNORECASE
                    ).strip()
            except Exception as e:
                logger.error(f"Error cancelando turno: {e}")

        # Detectar y ejecutar actualización de datos del paciente
        if telefono:
            try:
                logger.warning(f"[DIAG] detectar_actualizacion_dato — paciente_id={paciente_id_actual}, respuesta={respuesta[:60]}")
                actualizacion = detectar_actualizacion_dato(historial, respuesta, paciente_id=paciente_id_actual)
                if actualizacion:
                    from agent.tools import actualizar_paciente
                    datos = actualizacion["datos"]
                    paciente_id = actualizacion["paciente_id"]
                    resultado = await actualizar_paciente(paciente_id, datos)
                    logger.warning("[DIAG] Paciente actualizado: " + str(resultado))
            except Exception as e:
                logger.error(f"Error actualizando paciente: {e}")

        return respuesta
    except Exception as e:
        logger.error(f"Error Claude API: {e}")
        return obtener_mensaje_error()
