# agent/providers/meta.py — Adaptador para Meta Cloud API (WhatsApp Business)
import os
import json
import logging
import httpx
from fastapi import Request
from agent.providers.base import ProveedorWhatsApp, MensajeEntrante

logger = logging.getLogger("agentkit")


class ProveedorMeta(ProveedorWhatsApp):
    """Proveedor de WhatsApp usando Meta Cloud API."""

    def __init__(self):
        self.token = os.getenv("META_ACCESS_TOKEN")
        self.phone_number_id = os.getenv("META_PHONE_NUMBER_ID")
        self.verify_token = os.getenv("META_VERIFY_TOKEN", "sonria_verify")
        self.api_url = f"https://graph.facebook.com/v19.0/{self.phone_number_id}/messages"

    async def validar_webhook(self, request: Request):
        """Verificación GET requerida por Meta al configurar el webhook."""
        params = dict(request.query_params)
        mode = params.get("hub.mode")
        token = params.get("hub.verify_token")
        challenge = params.get("hub.challenge")

        if mode == "subscribe" and token == self.verify_token:
            logger.info("Webhook de Meta verificado correctamente")
            return int(challenge)

        logger.warning("Verificación de webhook Meta fallida")
        return None

    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        """Parsea el payload JSON de Meta Cloud API."""
        try:
            body = await request.json()
        except Exception:
            return []

        mensajes = []

        try:
            entries = body.get("entry", [])
            for entry in entries:
                for change in entry.get("changes", []):
                    value = change.get("value", {})

                    # Ignorar notificaciones de estado (delivered, read, etc.)
                    if "statuses" in value and "messages" not in value:
                        continue

                    for msg in value.get("messages", []):
                        # Solo procesar mensajes de texto
                        if msg.get("type") != "text":
                            continue

                        telefono = msg.get("from", "")
                        texto = msg.get("text", {}).get("body", "")
                        mensaje_id = msg.get("id", "")

                        if not texto:
                            continue

                        mensajes.append(MensajeEntrante(
                            telefono=telefono,
                            texto=texto,
                            mensaje_id=mensaje_id,
                            es_propio=False,
                        ))
        except Exception as e:
            logger.error(f"Error parseando webhook Meta: {e}")

        return mensajes

    async def enviar_mensaje(self, telefono: str, mensaje: str) -> bool:
        """Envía mensaje via Meta Cloud API."""
        if not all([self.token, self.phone_number_id]):
            logger.warning("Variables de Meta no configuradas (META_ACCESS_TOKEN, META_PHONE_NUMBER_ID)")
            return False

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

        data = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": telefono,
            "type": "text",
            "text": {"body": mensaje},
        }

        async with httpx.AsyncClient() as client:
            r = await client.post(self.api_url, headers=headers, json=data)
            if r.status_code != 200:
                logger.error(f"Error Meta API: {r.status_code} — {r.text}")
            return r.status_code == 200
