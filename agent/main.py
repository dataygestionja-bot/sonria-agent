# agent/main.py — Servidor FastAPI + Webhook de WhatsApp
# Generado por AgentKit

"""
Servidor principal del agente de WhatsApp.
Funciona con cualquier proveedor (Meta, Twilio) gracias a la capa de providers.
"""

import os
import sys
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv

from agent.brain import generar_respuesta
from agent.memory import inicializar_db, guardar_mensaje, obtener_historial, obtener_ultimo_timestamp, limpiar_historial
from agent.providers import obtener_proveedor
# from agent.notifications import scheduler, revisar_recordatorios  # DESACTIVADO TEMPORALMENTE

SESSION_TIMEOUT_HORAS = 6

load_dotenv()

# Configuración de logging según entorno
# stream=sys.stdout es necesario para Railway: todo lo que va a stderr
# se clasifica como "error" independientemente del nivel del log.
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
log_level = logging.DEBUG if ENVIRONMENT == "development" else logging.INFO
logging.basicConfig(
    level=log_level,
    stream=sys.stdout,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("agentkit")

# Proveedor de WhatsApp (se configura en .env con WHATSAPP_PROVIDER)
proveedor = obtener_proveedor()
PORT = int(os.getenv("PORT", 8000))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inicializa la base de datos y arranca el scheduler al arrancar el servidor."""
    await inicializar_db()
    logger.info("Base de datos inicializada")
    logger.info(f"Servidor AgentKit corriendo en puerto {PORT}")
    logger.info(f"Proveedor de WhatsApp: {proveedor.__class__.__name__}")

    # Scheduler de recordatorios — DESACTIVADO TEMPORALMENTE
    # scheduler.add_job(
    #     revisar_recordatorios,
    #     trigger="interval",
    #     minutes=15,
    #     args=[proveedor],
    #     id="recordatorios_turno",
    #     replace_existing=True,
    #     next_run_time=datetime.now(),
    # )
    # scheduler.start()
    logger.info("Scheduler de recordatorios DESACTIVADO")

    yield

    # scheduler.shutdown(wait=False)


app = FastAPI(
    title="AgentKit — WhatsApp AI Agent",
    version="1.0.0",
    lifespan=lifespan
)


@app.get("/")
async def health_check():
    """Endpoint de salud para Railway/monitoreo."""
    return {"status": "ok", "service": "agentkit"}


@app.get("/webhook")
async def webhook_verificacion(request: Request):
    """Verificación GET del webhook (requerido por Meta Cloud API, no-op para otros)."""
    resultado = await proveedor.validar_webhook(request)
    if resultado is not None:
        return PlainTextResponse(str(resultado))
    return {"status": "ok"}


_DIAS_ES = {0: "Lunes", 1: "Martes", 2: "Miércoles", 3: "Jueves",
            4: "Viernes", 5: "Sábado", 6: "Domingo"}


def _formatear_turno(turno: dict) -> str:
    """Retorna string legible: 'Jueves 22/05 a las 10:00 con Bruno Ordoñez'"""
    try:
        dt = datetime.strptime(turno["fecha"], "%Y-%m-%d")
        dia_nombre = _DIAS_ES[dt.weekday()]
        fecha_fmt = dt.strftime("%d/%m")
    except (ValueError, KeyError):
        dia_nombre = ""
        fecha_fmt = turno.get("fecha", "")
    hora_fmt = turno.get("hora_inicio", "")[:5]
    profesional = turno.get("profesional", "")
    return f"{dia_nombre} {fecha_fmt} a las {hora_fmt} con {profesional}".strip()


async def _cancelar_y_confirmar(turno: dict, telefono: str) -> str:
    """Cancela el turno y retorna el mensaje de confirmación."""
    from agent.tools import cancelar_turno
    await cancelar_turno(turno["id"])
    try:
        fecha_fmt = datetime.strptime(turno["fecha"], "%Y-%m-%d").strftime("%d/%m")
    except ValueError:
        fecha_fmt = turno.get("fecha", "")
    hora_fmt = turno.get("hora_inicio", "")[:5]
    logger.info(f"[CANCELO] Turno {turno['id']} cancelado para {telefono}")
    return (
        f"Tu turno del {fecha_fmt} a las {hora_fmt} fue cancelado. "
        f"¡Esperamos verte pronto! 😊"
    )


async def _manejar_cancelo(telefono: str) -> str:
    """
    Si el paciente tiene 1 turno próximo → cancela directo.
    Si tiene más de 1 → muestra lista numerada y espera elección.
    """
    from agent.tools import obtener_proximos_turnos_por_telefono
    turnos = await obtener_proximos_turnos_por_telefono(telefono)
    logger.info(f"[CANCELO] {telefono} — {len(turnos)} turno(s) encontrado(s): "
                f"{[t.get('id') for t in turnos]}")

    if not turnos:
        return "No encontré ningún turno próximo para cancelar 😊 ¿Hay algo más en que pueda ayudarte?"

    if len(turnos) == 1:
        return await _cancelar_y_confirmar(turnos[0], telefono)

    # Más de un turno → mostrar lista numerada y guardar estado en historial
    lineas = [f"{i+1}. {_formatear_turno(t)}" for i, t in enumerate(turnos)]
    return (
        "Tenés estos turnos próximos:\n"
        + "\n".join(lineas)
        + "\n¿Cuál querés cancelar? Respondé con el número."
    )


async def _manejar_seleccion_cancelo(telefono: str, num_str: str) -> str | None:
    """
    Si el último mensaje del asistente era una lista de CANCELO y el
    paciente respondió con un número, cancela el turno elegido.
    Retorna None si no estamos en ese contexto.

    El "estado" de espera está implícito en el historial: si el último
    mensaje del asistente contiene "¿Cuál querés cancelar?", cualquier
    número recibido se interpreta como selección de turno.
    """
    from agent.tools import obtener_proximos_turnos_por_telefono

    # Solo actuar si el mensaje es un número
    if not num_str.strip().isdigit():
        return None

    historial = await obtener_historial(telefono)
    ultimo_asistente = next(
        (m["content"] for m in reversed(historial) if m["role"] == "assistant"), ""
    )

    if "¿Cuál querés cancelar?" not in ultimo_asistente:
        return None

    idx = int(num_str.strip()) - 1
    turnos = await obtener_proximos_turnos_por_telefono(telefono)
    logger.info(f"[CANCELO-SELECCION] {telefono} eligió opción {num_str} "
                f"— {len(turnos)} turno(s) disponibles")

    if not turnos:
        return "No encontré turnos para cancelar. ¿Querés escribir CANCELO nuevamente?"

    if idx < 0 or idx >= len(turnos):
        return (f"Ese número no corresponde a ningún turno. "
                f"Respondé con un número del 1 al {len(turnos)}.")

    return await _cancelar_y_confirmar(turnos[idx], telefono)


_DESPEDIDAS = {
    "gracias", "chau", "chao", "bye", "hasta luego", "hasta pronto",
    "nos vemos", "listo", "ok gracias", "dale gracias", "muchas gracias",
    "nada más", "nada mas", "eso es todo", "todo bien", "estoy bien",
    "no necesito nada más", "no necesito nada mas", "no necesito más",
    "no necesito mas", "ya está", "ya esta", "perfecto gracias",
    "buenas noches", "buenas tardes", "buen día", "buen dia",
}


def _es_despedida(texto: str) -> bool:
    """Retorna True si el mensaje del paciente es una despedida."""
    t = texto.strip().lower()
    # Coincidencia exacta con el set
    if t in _DESPEDIDAS:
        return True
    # Frases cortas (≤ 5 palabras) que empiezan o son completamente una despedida
    palabras = t.split()
    if len(palabras) <= 5:
        for frase in _DESPEDIDAS:
            if t == frase or t.startswith(frase) or t.endswith(frase):
                return True
    return False


@app.post("/webhook")
async def webhook_handler(request: Request):
    """
    Recibe mensajes de WhatsApp via el proveedor configurado.
    Procesa el mensaje, genera respuesta con Claude y la envía de vuelta.
    """
    try:
        # Parsear webhook — el proveedor normaliza el formato
        mensajes = await proveedor.parsear_webhook(request)

        for msg in mensajes:
            # Ignorar mensajes propios o vacíos
            if msg.es_propio or not msg.texto:
                continue

            logger.info(f"Mensaje de {msg.telefono}: {msg.texto}")

            # ── Selección de turno a cancelar (respuesta al listado de CANCELO) ──
            respuesta_seleccion = await _manejar_seleccion_cancelo(msg.telefono, msg.texto.strip())
            if respuesta_seleccion is not None:
                await proveedor.enviar_mensaje(msg.telefono, respuesta_seleccion)
                await guardar_mensaje(msg.telefono, "user", msg.texto)
                await guardar_mensaje(msg.telefono, "assistant", respuesta_seleccion)
                logger.info(f"Selección CANCELO procesada para {msg.telefono}")
                continue

            # ── Detección de CANCELO (independiente del estado de la conversación) ──
            if msg.texto.strip().upper() == "CANCELO":
                respuesta = await _manejar_cancelo(msg.telefono)
                await proveedor.enviar_mensaje(msg.telefono, respuesta)
                await guardar_mensaje(msg.telefono, "user", msg.texto)
                await guardar_mensaje(msg.telefono, "assistant", respuesta)
                logger.info(f"CANCELO procesado para {msg.telefono}")
                continue

            # Verificar timeout de inactividad (6 horas)
            ultimo_ts = await obtener_ultimo_timestamp(msg.telefono)
            if ultimo_ts is not None:
                inactividad = datetime.utcnow() - ultimo_ts
                if inactividad > timedelta(hours=SESSION_TIMEOUT_HORAS):
                    logger.warning(
                        f"[SESSION] Timeout para {msg.telefono} — "
                        f"inactividad: {inactividad}. Limpiando historial."
                    )
                    mensaje_expiracion = (
                        "¡Hola! Tu sesión anterior expiró 😊 No hay problema, "
                        "estamos acá para ayudarte. ¿Me contás tu DNI para empezar?"
                    )
                    await proveedor.enviar_mensaje(msg.telefono, mensaje_expiracion)
                    await limpiar_historial(msg.telefono)

            # Obtener historial ANTES de guardar el mensaje actual
            # (brain.py agrega el mensaje actual, evitando duplicados)
            historial = await obtener_historial(msg.telefono)

            # Generar respuesta con Claude
            respuesta = await generar_respuesta(msg.texto, historial, telefono=msg.telefono)

            # Guardar mensaje del usuario Y respuesta del agente en memoria
            await guardar_mensaje(msg.telefono, "user", msg.texto)
            await guardar_mensaje(msg.telefono, "assistant", respuesta)

            # Enviar respuesta por WhatsApp via el proveedor
            await proveedor.enviar_mensaje(msg.telefono, respuesta)

            logger.info(f"Respuesta a {msg.telefono}: {respuesta}")

            # ── Cierre automático de sesión al despedirse ──────────────────────
            if _es_despedida(msg.texto):
                await limpiar_historial(msg.telefono)
                logger.info(f"[SESSION] Historial limpiado por despedida de {msg.telefono}")

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error en webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))
