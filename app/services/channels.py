"""Delivery channels behind one interface (11.2). Swap providers in config.py."""
import smtplib
from email.message import EmailMessage

import requests
from flask import current_app


class ChannelError(Exception):
    pass


class Channel:
    def send(self, notification):
        raise NotImplementedError


class ConsoleChannel(Channel):
    def __init__(self, name):
        self.name = name

    def send(self, notification):
        current_app.logger.info("[%s] to=%s subject=%s body=%s", self.name, notification.recipient,
                                notification.subject, notification.body)


class SmtpEmailChannel(Channel):  # pragma: no cover - network
    def send(self, notification):
        cfg = current_app.config
        msg = EmailMessage()
        msg["From"] = cfg["MAIL_FROM"]
        msg["To"] = notification.recipient
        msg["Subject"] = notification.subject or cfg["APP_NAME"]
        body = notification.body
        if notification.link:
            body += f"\n\n{notification.link}"
        msg.set_content(body)
        try:
            with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"],
                              timeout=cfg["EXTERNAL_API_TIMEOUT_SECONDS"]) as smtp:
                if cfg["SMTP_USE_TLS"]:
                    smtp.starttls()
                if cfg["SMTP_USERNAME"]:
                    smtp.login(cfg["SMTP_USERNAME"], cfg["SMTP_PASSWORD"])
                smtp.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            raise ChannelError(str(exc)) from exc


class Msg91SmsChannel(Channel):  # pragma: no cover - network
    def send(self, notification):
        cfg = current_app.config
        template_id = cfg["MSG91_TEMPLATE_IDS"].get(notification.event.value)
        if not template_id:
            raise ChannelError(f"No DLT template registered for {notification.event.value}")
        payload = {"template_id": template_id, "sender": cfg["MSG91_SENDER_ID"],
                   "recipients": [{"mobiles": notification.recipient, **(notification.payload or {})}]}
        try:
            resp = requests.post(cfg["MSG91_FLOW_URL"], json=payload,
                                 headers={"authkey": cfg["MSG91_AUTH_KEY"]},
                                 timeout=cfg["EXTERNAL_API_TIMEOUT_SECONDS"])
        except requests.RequestException as exc:
            raise ChannelError(str(exc)) from exc
        if resp.status_code >= 300:
            raise ChannelError(f"MSG91 returned {resp.status_code}")


class WhatsAppCloudChannel(Channel):  # pragma: no cover - network
    def send(self, notification):
        cfg = current_app.config
        template = cfg["WHATSAPP_TEMPLATE_NAMES"].get(notification.event.value)
        if not template:
            raise ChannelError(f"No approved WhatsApp template for {notification.event.value}")
        url = f"{cfg['WHATSAPP_API_URL']}/{cfg['WHATSAPP_PHONE_NUMBER_ID']}/messages"
        params = [{"type": "text", "text": str(v)} for v in (notification.payload or {}).values()]
        body = {"messaging_product": "whatsapp", "to": notification.recipient, "type": "template",
                "template": {"name": template, "language": {"code": "en"},
                             "components": [{"type": "body", "parameters": params}]}}
        try:
            resp = requests.post(url, json=body, timeout=cfg["EXTERNAL_API_TIMEOUT_SECONDS"],
                                 headers={"Authorization": f"Bearer {cfg['WHATSAPP_ACCESS_TOKEN']}"})
        except requests.RequestException as exc:
            raise ChannelError(str(exc)) from exc
        if resp.status_code >= 300:
            raise ChannelError(f"WhatsApp API returned {resp.status_code}")


class InAppChannel(Channel):
    def send(self, notification):
        return None  # the row itself is the in-app notification


def channel_for(channel_name):
    cfg = current_app.config
    if channel_name == "IN_APP":
        return InAppChannel()
    if channel_name == "EMAIL":
        return SmtpEmailChannel() if cfg["EMAIL_BACKEND"] == "smtp" else ConsoleChannel("EMAIL")
    if channel_name == "SMS":
        return Msg91SmsChannel() if cfg["SMS_BACKEND"] == "msg91" else ConsoleChannel("SMS")
    if channel_name == "WHATSAPP":
        return WhatsAppCloudChannel() if cfg["WHATSAPP_BACKEND"] == "cloud_api" else ConsoleChannel("WHATSAPP")
    raise ChannelError(f"Unknown channel {channel_name}")
