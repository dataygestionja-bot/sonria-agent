# agent/tools.py — Herramientas del agente
# Conectado a Supabase del consultorio Data y Gestión

import os
import yaml
import logging
import httpx
from datetime import datetime, date, timedelta
from typing import Optional

logger = logging.getLogger("agentkit")

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://wgzbbylwawelmxzrgdiw.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6IndnemJieWx3YXdlbG14enJnZGl3Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzkxMjg1NDgsImV4cCI6MjA5NDcwNDU0OH0.GJ-KV1NW8ckmwkfbo-HA347NJjfJ0f3AM34rh8exZpc")
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

# Nombres canónicos por UUID (evita problemas si la DB tiene nombre/apellido invertidos)
NOMBRES_CANONICOS = {
    "b5af188f-aa9e-4983-8365-92930cbc9eeb": "Bruno Ordoñez",
    "9cd6412e-e1e9-4b20-aa78-a9ba03ea240d": "Federico Cabrera",
    "318bdbf8-04dc-4953-b284-d3c5f429cbbf": "Florencia Celsi",
    "3b90bf47-16be-4116-b348-fd1bf2b9ef8c": "Fernando Rojas",
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


async def supabase_patch(tabla: str, filtro: dict, data: dict) -> dict:
    url = f"{SUPABASE_URL}/rest/v1/{tabla}"
    headers_patch = {**HEADERS, "Prefer": "return=representation"}
    async with httpx.AsyncClient() as client:
        r = await client.patch(url, headers=headers_patch, params=filtro, json=data)
        if r.status_code in (200, 204):
            try:
                resultado = r.json()
                if isinstance(resultado, list) and resultado:
                    return resultado[0]
                return {"ok": True}
            except Exception:
                return {"ok": True}
        logger.error(f"Supabase PATCH error {r.status_code}: {r.text}")
        return {}


# ─── Pacientes ────────────────────────────────────────────────────────────────

async def buscar_paciente_por_dni(dni: str) -> dict | None:
    """Busca un paciente por DNI. Retorna el paciente con nombre de obra social o None."""
    resultados = await supabase_get("pacientes", {
        "dni": f"eq.{dni.strip()}",
        "activo": "eq.true",
        "select": "id,nombre,apellido,dni,telefono,obra_social_id"
    })
    if not resultados:
        return None

    paciente = resultados[0]

    # Obtener nombre de obra social si tiene
    if paciente.get("obra_social_id"):
        obras = await supabase_get("obras_sociales", {
            "id": f"eq.{paciente['obra_social_id']}",
            "select": "nombre"
        })
        if obras:
            paciente["obra_social_nombre"] = obras[0]["nombre"]

    return paciente


async def buscar_paciente_por_telefono(telefono: str) -> dict | None:
    """Busca un paciente por número de teléfono."""
    # Normalizar teléfono — quitar + y buscar variantes
    telefono_limpio = telefono.replace("+", "").replace(" ", "")

    resultados = await supabase_get("pacientes", {
        "telefono": f"ilike.*{telefono_limpio[-10:]}*",
        "activo": "eq.true",
        "select": "id,nombre,apellido,dni,telefono,obra_social_id"
    })
    if not resultados:
        return None

    paciente = resultados[0]

    if paciente.get("obra_social_id"):
        obras = await supabase_get("obras_sociales", {
            "id": f"eq.{paciente['obra_social_id']}",
            "select": "nombre"
        })
        if obras:
            paciente["obra_social_nombre"] = obras[0]["nombre"]

    return paciente


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
        "pendiente_validacion": True,
    }
    if obra_social_id:
        paciente["obra_social_id"] = obra_social_id

    resultado = await supabase_post("pacientes", paciente)
    if resultado.get("id"):
        logger.info(f"Paciente creado: {resultado['id']}")
    return resultado


OBRAS_SOCIALES_IDS = {
    "galeno": "6bbf5093-74ec-4dbf-a02e-edf5f26374df",
    "osde": "6c4be89c-ecdc-4cf0-abff-556308f43037",
    "osecac": "0fae4ef7-e94d-4f7e-8033-abdad690686c",
    "ospe": "8699bc98-dd98-45d6-99e0-6c6b65562fc2",
    "sancor salud": "5de29dcc-4e4a-4ef7-9fc3-85f250f2a986",
    "swiss medical": "3d817068-8268-45f7-bca8-ff6abbfbc343",
}


async def buscar_obra_social_id(nombre_obra: str) -> str | None:
    nombre_lower = nombre_obra.strip().lower()
    if nombre_lower in OBRAS_SOCIALES_IDS:
        return OBRAS_SOCIALES_IDS[nombre_lower]
    for clave, obra_id in OBRAS_SOCIALES_IDS.items():
        if clave in nombre_lower or nombre_lower in clave:
            return obra_id
    return None


async def actualizar_paciente(paciente_id: str, datos: dict) -> dict:
    """Actualiza datos de un paciente existente."""
    resultado = await supabase_patch(
        "pacientes",
        {"id": f"eq.{paciente_id}"},
        datos
    )
    logger.info(f"Paciente actualizado: {paciente_id}")
    return resultado


async def actualizar_obra_social_paciente(paciente_id: str, nombre_obra_social: str) -> dict:
    obra_social_id = await buscar_obra_social_id(nombre_obra_social)
    if not obra_social_id:
        return {"exito": False, "mensaje": "Obra social no encontrada."}

    resultado = await supabase_patch(
        "pacientes",
        {"id": f"eq.{paciente_id}"},
        {"obra_social_id": obra_social_id}
    )
    return {
        "exito": True,
        "mensaje": f"Obra social actualizada a {nombre_obra_social}.",
        "obra_social_id": obra_social_id
    }


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
    paciente = await buscar_paciente_por_dni(dni)
    if paciente:
        logger.info(f"Paciente encontrado: {paciente['id']}")
        return paciente

    obra_social_id = None
    if obra_social and obra_social.lower() != "particular":
        obra_social_id = await buscar_obra_social_id(obra_social)

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
    resultado = [p for p in todos if especialidad.lower() in p.get("especialidad", "").lower()]
    # Normalizar nombre canónico si la DB lo tiene invertido
    for p in resultado:
        if p["id"] in NOMBRES_CANONICOS:
            partes = NOMBRES_CANONICOS[p["id"]].split(maxsplit=1)
            p["nombre"] = partes[0]
            p["apellido"] = partes[1] if len(partes) > 1 else ""
    return resultado


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


async def obtener_bloqueos_fecha(profesional_id: str, fecha_str: str) -> list[dict]:
    """
    Retorna los bloqueos activos del profesional que cubren fecha_str.
    Un bloqueo cubre la fecha si: fecha_desde <= fecha_str <= fecha_hasta.
    """
    bloqueos = await supabase_get("bloqueos_agenda", {
        "profesional_id": f"eq.{profesional_id}",
        "estado": "eq.activo",
        "fecha_desde": f"lte.{fecha_str}",
        "fecha_hasta": f"gte.{fecha_str}",
        "select": "todo_el_dia,hora_desde,hora_hasta,motivo",
    })
    return bloqueos


def _slot_bloqueado(slot_str: str, duracion_min: int, bloqueos: list[dict]) -> bool:
    """
    Retorna True si el slot debe ser excluido por algún bloqueo.

    - todo_el_dia=True  → bloquea siempre
    - todo_el_dia=False → bloquea solo si el slot se superpone con
                          [hora_desde, hora_hasta) del bloqueo
    """
    slot_ini = datetime.strptime(slot_str, "%H:%M")
    slot_fin = slot_ini + timedelta(minutes=duracion_min)

    for b in bloqueos:
        if b.get("todo_el_dia"):
            return True
        # Bloqueo parcial: verificar solapamiento
        h_desde = b.get("hora_desde")
        h_hasta = b.get("hora_hasta")
        if h_desde and h_hasta:
            blq_ini = datetime.strptime(str(h_desde)[:5], "%H:%M")
            blq_fin = datetime.strptime(str(h_hasta)[:5], "%H:%M")
            # Hay solapamiento si slot_ini < blq_fin AND slot_fin > blq_ini
            if slot_ini < blq_fin and slot_fin > blq_ini:
                return True
    return False


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

    # Verificar bloqueos de agenda para esta fecha
    bloqueos = await obtener_bloqueos_fecha(profesional_id, fecha_str)

    # Si hay algún bloqueo de todo el día, la fecha entera no está disponible
    if any(b.get("todo_el_dia") for b in bloqueos):
        logger.info(f"Fecha {fecha_str} bloqueada (todo el día) para prof {profesional_id}")
        return []

    ocupados = await obtener_turnos_ocupados(profesional_id, fecha_str)
    ocupados_inicio = {t[0][:5] for t in ocupados}

    inicio = datetime.strptime(horario_dia["hora_inicio"][:5], "%H:%M")
    fin = datetime.strptime(horario_dia["hora_fin"][:5], "%H:%M")
    duracion_min = horario_dia["duracion_slot_min"]
    duracion = timedelta(minutes=duracion_min)

    slots = []
    actual = inicio
    while actual + duracion <= fin:
        slot_str = actual.strftime("%H:%M")
        if slot_str not in ocupados_inicio and not _slot_bloqueado(slot_str, duracion_min, bloqueos):
            slots.append(slot_str)
        actual += duracion

    return slots


async def obtener_proximas_fechas_disponibles(profesional_id: str, dias_a_buscar: int = 14) -> list[dict]:
    horarios = await obtener_horarios_profesional(profesional_id)
    dias_con_horario = {h["dia_semana"] for h in horarios}
    logger.warning(f"[DIAG] disponibilidad prof={profesional_id} dias_horario={dias_con_horario}")

    resultados = []
    hoy = date.today()

    for i in range(0, dias_a_buscar + 1):
        fecha = hoy + timedelta(days=i)
        dia_iso = fecha.isoweekday()

        if dia_iso not in dias_con_horario:
            continue

        fecha_str = fecha.strftime("%Y-%m-%d")
        slots = await obtener_slots_disponibles(profesional_id, fecha_str)
        logger.warning(f"[DIAG] {fecha_str} dia_iso={dia_iso} slots={slots}")

        if slots:
            resultados.append({
                "fecha": fecha_str,
                "dia_nombre": DIAS_SEMANA[fecha.isoweekday() - 1],
                "slots": slots[:6]
            })

        if len(resultados) >= 3:
            break

    logger.warning(f"[DIAG] disponibilidad resultado={resultados}")

    return resultados


async def obtener_turnos_pendientes_recordatorio(
    fecha: str,
    campo_enviado: str,
) -> list[dict]:
    """
    Retorna los turnos confirmados de una fecha que aún no recibieron
    el recordatorio indicado (campo_enviado = 'recordatorio_24h_enviado'
    o 'recordatorio_2h_enviado').
    Enriquece cada turno con nombre_paciente, telefono y profesional.
    """
    turnos = await supabase_get("turnos", {
        "fecha": f"eq.{fecha}",
        "estado": "eq.confirmado",
        campo_enviado: "eq.false",
        "select": (
            "id,fecha,hora_inicio,paciente_id,profesional_id,"
            "nombre_solicitante,telefono_solicitante"
        ),
        "order": "hora_inicio.asc",
    })

    for t in turnos:
        # Teléfono: usar el del solicitante o buscar en la ficha del paciente
        telefono = t.get("telefono_solicitante", "") or ""
        nombre = t.get("nombre_solicitante", "") or ""

        if (not telefono or not nombre) and t.get("paciente_id"):
            pacientes = await supabase_get("pacientes", {
                "id": f"eq.{t['paciente_id']}",
                "select": "nombre,telefono",
            })
            if pacientes:
                if not nombre:
                    nombre = pacientes[0].get("nombre", "")
                if not telefono:
                    telefono = pacientes[0].get("telefono", "")

        t["nombre_paciente"] = nombre.strip()
        t["telefono"] = telefono.strip()

        # Nombre canónico del profesional
        prof_id = t.get("profesional_id")
        t["profesional"] = NOMBRES_CANONICOS.get(prof_id, "") if prof_id else ""

    return turnos


async def marcar_recordatorio_enviado(turno_id: str, campo: str) -> dict:
    """Marca true la columna de recordatorio correspondiente en el turno."""
    return await supabase_patch(
        "turnos",
        {"id": f"eq.{turno_id}"},
        {campo: True},
    )


async def obtener_proximo_turno_por_telefono(telefono: str) -> dict | None:
    """
    Busca el próximo turno confirmado de un paciente por su número de teléfono.
    Busca primero por telefono_solicitante y luego por paciente vinculado.
    """
    hoy = date.today().strftime("%Y-%m-%d")
    telefono_limpio = telefono.replace("+", "").replace(" ", "")
    sufijo = telefono_limpio[-10:]  # últimos 10 dígitos para tolerancia de prefijos

    # Búsqueda por teléfono del solicitante
    turnos = await supabase_get("turnos", {
        "telefono_solicitante": f"ilike.*{sufijo}*",
        "fecha": f"gte.{hoy}",
        "estado": "eq.confirmado",
        "select": "id,fecha,hora_inicio,profesional_id",
        "order": "fecha.asc,hora_inicio.asc",
        "limit": "1",
    })

    if not turnos:
        # Fallback: buscar por paciente_id
        paciente = await buscar_paciente_por_telefono(telefono)
        if paciente:
            turnos = await supabase_get("turnos", {
                "paciente_id": f"eq.{paciente['id']}",
                "fecha": f"gte.{hoy}",
                "estado": "eq.confirmado",
                "select": "id,fecha,hora_inicio,profesional_id",
                "order": "fecha.asc,hora_inicio.asc",
                "limit": "1",
            })

    if not turnos:
        return None

    t = turnos[0]
    prof_id = t.get("profesional_id")
    t["profesional"] = NOMBRES_CANONICOS.get(prof_id, "") if prof_id else ""
    return t


async def obtener_proximos_turnos_por_telefono(telefono: str) -> list[dict]:
    """
    Igual que obtener_proximo_turno_por_telefono pero retorna TODOS
    los turnos confirmados futuros (hasta 10), ordenados por fecha/hora.
    """
    hoy = date.today().strftime("%Y-%m-%d")
    telefono_limpio = telefono.replace("+", "").replace(" ", "")
    sufijo = telefono_limpio[-10:]

    turnos = await supabase_get("turnos", {
        "telefono_solicitante": f"ilike.*{sufijo}*",
        "fecha": f"gte.{hoy}",
        "estado": "eq.confirmado",
        "select": "id,fecha,hora_inicio,profesional_id",
        "order": "fecha.asc,hora_inicio.asc",
        "limit": "10",
    })

    if not turnos:
        paciente = await buscar_paciente_por_telefono(telefono)
        if paciente:
            turnos = await supabase_get("turnos", {
                "paciente_id": f"eq.{paciente['id']}",
                "fecha": f"gte.{hoy}",
                "estado": "eq.confirmado",
                "select": "id,fecha,hora_inicio,profesional_id",
                "order": "fecha.asc,hora_inicio.asc",
                "limit": "10",
            })

    for t in turnos:
        prof_id = t.get("profesional_id")
        t["profesional"] = NOMBRES_CANONICOS.get(prof_id, "") if prof_id else ""

    return turnos


async def listar_obras_sociales() -> dict:
    resultados = await supabase_get("obras_sociales", {
        "activo": "eq.true",
        "select": "id,nombre",
        "order": "nombre"
    })
    return {"obras_sociales": resultados}


async def obtener_obras_sociales() -> list[str]:
    os_list = await supabase_get("obras_sociales", {"activo": "eq.true", "select": "nombre"})
    return [o["nombre"] for o in os_list]


# ─── Turnos del paciente ──────────────────────────────────────────────────────

async def obtener_turnos_paciente(paciente_id: str) -> list[dict]:
    """Trae los turnos futuros confirmados del paciente."""
    hoy = date.today().strftime("%Y-%m-%d")
    turnos = await supabase_get("turnos", {
        "paciente_id": f"eq.{paciente_id}",
        "fecha": f"gte.{hoy}",
        "estado": "eq.confirmado",
        "select": "id,fecha,hora_inicio,motivo_consulta,profesional_id",
        "order": "fecha.asc"
    })
    for t in turnos:
        prof_id = t.get("profesional_id")
        if prof_id:
            # Usar nombre canónico hardcodeado si está disponible (evita orden invertido en DB)
            t["profesional"] = NOMBRES_CANONICOS.get(prof_id, prof_id)
    return turnos


async def cancelar_turno(turno_id: str) -> dict:
    """Cambia el estado del turno a cancelado."""
    resultado = await supabase_patch(
        "turnos",
        {"id": f"eq.{turno_id}"},
        {"estado": "cancelado"}
    )
    logger.info(f"Turno cancelado: {turno_id}")
    return resultado


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
    """Busca o crea el paciente por DNI y registra el turno en Supabase."""
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
