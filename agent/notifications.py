# agent/notifications.py — Recordatorios automáticos de turnos
"""
Scheduler que corre cada 15 minutos dentro del proceso FastAPI.
Envía recordatorios de turno por WhatsApp:
  - 24 horas antes  → columna recordatorio_24h_enviado
  - 2 horas antes   → columna recordatorio_2h_enviado
"""

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger("agentkit")

TZ_BA = ZoneInfo("America/Argentina/Buenos_Aires")

# Instancia global — se arranca/para en el lifespan de FastAPI
scheduler = AsyncIOScheduler(timezone="America/Argentina/Buenos_Aires")


# ─── helpers ──────────────────────────────────────────────────────────────────

def _combinar_fecha_hora(fecha_str: str, hora_str: str) -> datetime | None:
    """Combina 'YYYY-MM-DD' y 'HH:MM[:SS]' en un datetime con TZ de Buenos Aires."""
    try:
        hora_hm = hora_str[:5]  # tomar solo HH:MM
        dt = datetime.strptime(f"{fecha_str} {hora_hm}", "%Y-%m-%d %H:%M")
        return dt.replace(tzinfo=TZ_BA)
    except ValueError:
        return None


async def _enviar_recordatorio(proveedor, turno: dict, tipo: str) -> None:
    """Formatea y envía un recordatorio; marca la columna correspondiente."""
    from agent.tools import marcar_recordatorio_enviado

    turno_id = turno.get("id")
    telefono = turno.get("telefono", "")
    nombre = turno.get("nombre_paciente", "")
    hora_fmt = turno.get("hora_inicio", "")[:5]
    profesional = turno.get("profesional", "el profesional")
    fecha_str = turno.get("fecha", "")

    if not telefono:
        logger.warning(f"[RECORDATORIO-{tipo}] Turno {turno_id} sin teléfono — omitido")
        return

    if tipo == "24h":
        try:
            dia_fmt = datetime.strptime(fecha_str, "%Y-%m-%d").strftime("%d/%m")
        except ValueError:
            dia_fmt = fecha_str
        texto = (
            f"Hola {nombre} 👋 Te recordamos tu turno mañana {dia_fmt} "
            f"a las {hora_fmt} con {profesional}. "
            f"Si no podés asistir respondé CANCELO."
        )
        campo = "recordatorio_24h_enviado"
    else:
        texto = (
            f"Hola {nombre} 😊 Tu turno es hoy a las {hora_fmt} "
            f"con {profesional}. "
            f"Si no podés asistir respondé CANCELO."
        )
        campo = "recordatorio_2h_enviado"

    from agent.tools import log_bot_event
    ok = await proveedor.enviar_mensaje(telefono, texto)
    if ok:
        await marcar_recordatorio_enviado(turno_id, campo)
        logger.info(f"[RECORDATORIO-{tipo}] ✓ {telefono} — turno {turno_id}")
        asyncio.create_task(log_bot_event(
            tipo="recordatorio_enviado",
            nivel="info",
            telefono=telefono,
            turno_id=turno_id or "",
            detalle=f"Recordatorio {tipo} enviado para turno del {fecha_str} a las {hora_fmt} con {profesional}",
        ))
    else:
        logger.warning(f"[RECORDATORIO-{tipo}] ✗ fallo envío {telefono} — turno {turno_id}")


# ─── job principal ────────────────────────────────────────────────────────────

async def revisar_recordatorios(proveedor) -> None:
    """
    Corre cada 15 minutos. Busca turnos confirmados que necesiten
    recordatorio de 24h o de 2h y los envía si aún no fueron enviados.

    Ventanas de tiempo (con margen de ±30 min para tolerar jitter del scheduler):
      - 24h: turno_datetime entre ahora+23h y ahora+25h
      - 2h:  turno_datetime entre ahora+1h30 y ahora+2h30
    """
    from agent.tools import obtener_turnos_pendientes_recordatorio

    ahora = datetime.now(TZ_BA)
    hoy = ahora.date()
    manana = hoy + timedelta(days=1)

    logger.info(f"[RECORDATORIOS] Revisión a las {ahora.strftime('%H:%M')} (BA)")

    # ── Recordatorio 24h (turnos de mañana) ───────────────────────────────────
    turnos_24h = await obtener_turnos_pendientes_recordatorio(
        fecha=str(manana),
        campo_enviado="recordatorio_24h_enviado",
    )
    logger.info(f"[SCHEDULER-DIAG] 24h: {len(turnos_24h)} turno(s) candidato(s) para mañana {manana}")
    for t in turnos_24h:
        dt_turno = _combinar_fecha_hora(t.get("fecha", ""), t.get("hora_inicio", ""))
        if dt_turno is None:
            logger.warning(f"[SCHEDULER-DIAG] 24h: Turno {t.get('id')} — fecha/hora inválida, omitido")
            continue
        diff_h = (dt_turno - ahora).total_seconds() / 3600
        logger.info(f"[SCHEDULER-DIAG] 24h: Turno {t.get('id')} "
                    f"hora={t.get('hora_inicio')} tel={t.get('telefono')!r} "
                    f"diff_h={diff_h:.2f} — {'ENVIAR' if 23 <= diff_h <= 25 else 'fuera de ventana'}")
        if 23 <= diff_h <= 25:
            await _enviar_recordatorio(proveedor, t, "24h")

    # ── Recordatorio 2h (turnos de hoy) ───────────────────────────────────────
    turnos_2h = await obtener_turnos_pendientes_recordatorio(
        fecha=str(hoy),
        campo_enviado="recordatorio_2h_enviado",
    )
    logger.info(f"[SCHEDULER-DIAG] 2h: {len(turnos_2h)} turno(s) candidato(s) para hoy {hoy}")
    for t in turnos_2h:
        dt_turno = _combinar_fecha_hora(t.get("fecha", ""), t.get("hora_inicio", ""))
        if dt_turno is None:
            logger.warning(f"[SCHEDULER-DIAG] 2h: Turno {t.get('id')} — fecha/hora inválida, omitido")
            continue
        diff_h = (dt_turno - ahora).total_seconds() / 3600
        logger.info(f"[SCHEDULER-DIAG] 2h: Turno {t.get('id')} "
                    f"hora={t.get('hora_inicio')} tel={t.get('telefono')!r} "
                    f"diff_h={diff_h:.2f} — {'ENVIAR' if 1.5 <= diff_h <= 2.5 else 'fuera de ventana'}")
        if 1.5 <= diff_h <= 2.5:
            await _enviar_recordatorio(proveedor, t, "2h")
