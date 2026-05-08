# agent/tools.py — Herramientas del agente
# Generado por AgentKit

"""
Herramientas específicas del consultorio Data y Gestión.
Casos de uso configurados: FAQ + Agendar citas.
"""

import os
import json
import yaml
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger("agentkit")


def cargar_info_negocio() -> dict:
    """Carga la información del negocio desde business.yaml."""
    try:
        with open("config/business.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logger.error("config/business.yaml no encontrado")
        return {}


# ════════════════════════════════════════════════════════════
# Herramientas de FAQ — buscar en knowledge base
# ════════════════════════════════════════════════════════════

def buscar_en_knowledge(consulta: str) -> str:
    """
    Busca información relevante en los archivos de /knowledge.
    Retorna el contenido más relevante encontrado.
    """
    resultados = []
    knowledge_dir = "knowledge"

    if not os.path.exists(knowledge_dir):
        return "No hay archivos de conocimiento disponibles."

    for archivo in os.listdir(knowledge_dir):
        ruta = os.path.join(knowledge_dir, archivo)
        if archivo.startswith(".") or not os.path.isfile(ruta):
            continue
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                contenido = f.read()
                if consulta.lower() in contenido.lower():
                    resultados.append(f"[{archivo}]: {contenido[:500]}")
        except (UnicodeDecodeError, IOError):
            continue

    if resultados:
        return "\n---\n".join(resultados)
    return "No encontré información específica sobre eso en mis archivos."


def obtener_horario() -> dict:
    """Retorna el horario de atención y si está abierto en este momento."""
    info = cargar_info_negocio()
    horario_str = info.get("negocio", {}).get("horario", "No disponible")

    # Cálculo simple: L-V 9-19, Sab 9-13, Dom cerrado
    ahora = datetime.now()
    dia_semana = ahora.weekday()  # 0=Lunes, 6=Domingo
    hora = ahora.hour

    if dia_semana <= 4:  # Lunes a Viernes
        esta_abierto = 9 <= hora < 19
    elif dia_semana == 5:  # Sábado
        esta_abierto = 9 <= hora < 13
    else:  # Domingo
        esta_abierto = False

    return {
        "horario": horario_str,
        "esta_abierto": esta_abierto,
        "dia_actual": ["Lunes", "Martes", "Miércoles", "Jueves",
                       "Viernes", "Sábado", "Domingo"][dia_semana],
    }


def obtener_servicios() -> list:
    """Retorna la lista de servicios que ofrece el consultorio."""
    info = cargar_info_negocio()
    return info.get("negocio", {}).get("servicios", [])


def obtener_ubicacion() -> str:
    """Retorna la ubicación del consultorio."""
    info = cargar_info_negocio()
    return info.get("negocio", {}).get("ubicacion", "La Plata, Buenos Aires")


# ════════════════════════════════════════════════════════════
# Herramientas de agendamiento de citas
# ════════════════════════════════════════════════════════════

# Archivo donde se guardan las solicitudes de turno (formato simple JSONL)
SOLICITUDES_FILE = "solicitudes_turnos.jsonl"


def registrar_solicitud_turno(
    telefono: str,
    nombre: str,
    motivo: str,
    cobertura: str,
    preferencia_horaria: Optional[str] = None,
) -> dict:
    """
    Registra una solicitud de turno para que el equipo la revise y confirme.
    NO confirma el turno directamente — esa decisión la toma una persona del equipo.

    Args:
        telefono: Número del paciente
        nombre: Nombre y apellido
        motivo: Motivo de consulta o tratamiento
        cobertura: "particular" u obra social
        preferencia_horaria: Día/franja preferida (opcional)

    Returns:
        dict con el ID de la solicitud y un mensaje de confirmación
    """
    solicitud = {
        "id": f"TURNO-{datetime.now().strftime('%Y%m%d%H%M%S')}",
        "telefono": telefono,
        "nombre": nombre,
        "motivo": motivo,
        "cobertura": cobertura,
        "preferencia_horaria": preferencia_horaria,
        "estado": "pendiente",
        "creado_en": datetime.now().isoformat(),
    }

    try:
        with open(SOLICITUDES_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(solicitud, ensure_ascii=False) + "\n")
        logger.info(f"Solicitud de turno registrada: {solicitud['id']}")
    except IOError as e:
        logger.error(f"Error guardando solicitud: {e}")
        return {"ok": False, "mensaje": "No pude registrar la solicitud."}

    return {
        "ok": True,
        "id": solicitud["id"],
        "mensaje": (
            f"Solicitud {solicitud['id']} registrada. "
            f"El equipo se contactará para confirmar día y hora."
        ),
    }


def listar_solicitudes_pendientes() -> list:
    """Retorna las solicitudes de turno pendientes (para uso del equipo)."""
    if not os.path.exists(SOLICITUDES_FILE):
        return []

    pendientes = []
    try:
        with open(SOLICITUDES_FILE, "r", encoding="utf-8") as f:
            for linea in f:
                linea = linea.strip()
                if not linea:
                    continue
                try:
                    solicitud = json.loads(linea)
                    if solicitud.get("estado") == "pendiente":
                        pendientes.append(solicitud)
                except json.JSONDecodeError:
                    continue
    except IOError as e:
        logger.error(f"Error leyendo solicitudes: {e}")

    return pendientes
