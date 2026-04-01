"""
threads_client.py — Cliente para Threads usando Meta Graph API
Docs: https://developers.facebook.com/docs/threads
Requiere: pip install requests
"""
import requests
import logging
from pathlib import Path
from config import ThreadsConfig

log = logging.getLogger(__name__)

GRAPH_URL = "https://graph.threads.net/v1.0"


class ThreadsClient:
    def __init__(self, cfg: ThreadsConfig):
        self.token = cfg.access_token
        self.user_id = cfg.user_id

    def _get(self, path: str, params: dict = None) -> dict:
        params = params or {}
        params["access_token"] = self.token
        r = requests.get(f"{GRAPH_URL}/{path}", params=params, timeout=10)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, data: dict = None) -> dict:
        data = data or {}
        data["access_token"] = self.token
        r = requests.post(f"{GRAPH_URL}/{path}", data=data, timeout=10)
        if not r.ok:
            log.error(f"[Threads] POST {path} → {r.status_code}: {r.text}")
        r.raise_for_status()
        return r.json()

    # ── Publicar ──────────────────────────────────────────────────────────────

    def post(self, text: str, media_path: str = None, media_type: str = "TEXT") -> dict:
        """
        Publica en Threads. Proceso en 2 pasos: crear contenedor → publicar.
        media_type: "TEXT" | "IMAGE" | "VIDEO"
        """
        # Paso 1: Crear contenedor de media
        container_data = {
            "media_type": media_type,
            "text": text,
        }

        if media_path and Path(media_path).exists() and media_type != "TEXT":
            # Para imagen/video debes subir a una URL pública primero.
            # Aquí asumimos que media_path ya es una URL pública.
            if media_type == "IMAGE":
                container_data["image_url"] = media_path
            elif media_type == "VIDEO":
                container_data["video_url"] = media_path

        container = self._post(f"{self.user_id}/threads", container_data)
        container_id = container.get("id")
        if not container_id:
            raise ValueError(f"No se obtuvo container_id: {container}")

        # Paso 2: Publicar el contenedor
        result = self._post(f"{self.user_id}/threads_publish", {"creation_id": container_id})
        post_id = result.get("id")
        log.info(f"[Threads] Post publicado: {post_id}")
        return {"id": post_id, "url": f"https://www.threads.net/@me/post/{post_id}"}

    def reply(self, text: str, reply_to_id: str) -> dict:
        """Responde a un post de Threads."""
        container = self._post(f"{self.user_id}/threads", {
            "media_type": "TEXT",
            "text": text,
            "reply_to_id": reply_to_id,
        })
        container_id = container.get("id")
        result = self._post(f"{self.user_id}/threads_publish", {"creation_id": container_id})
        log.info(f"[Threads] Reply enviado a {reply_to_id}")
        return {"id": result.get("id")}

    # ── Menciones ─────────────────────────────────────────────────────────────

    def get_recent_replies(self, since_timestamp: int = None) -> list[dict]:
        """Obtiene respuestas/menciones recientes al usuario."""
        params = {
            "fields": "id,text,timestamp,username",
            "limit": 20,
        }
        try:
            data = self._get(f"{self.user_id}/replies", params)
            mentions = data.get("data", [])
            if since_timestamp:
                mentions = [m for m in mentions if int(m.get("timestamp", 0)) > since_timestamp]
            return mentions
        except Exception as e:
            log.error(f"[Threads] Error al obtener replies: {e}")
            return []

    # ── Métricas ──────────────────────────────────────────────────────────────

    def get_post_metrics(self, post_id: str) -> dict:
        """Obtiene métricas de un post de Threads."""
        try:
            data = self._get(f"{post_id}/insights", {
                "metric": "likes,replies,reposts,views",
                "period": "lifetime",
            })
            result = {}
            for item in data.get("data", []):
                name = item.get("name")
                value = item.get("values", [{}])[-1].get("value", 0)
                result[name] = value
            return {
                "likes": result.get("likes", 0),
                "reposts": result.get("reposts", 0),
                "replies": result.get("replies", 0),
                "impressions": result.get("views", 0),
            }
        except Exception as e:
            log.error(f"[Threads] Error métricas {post_id}: {e}")
            return {}

    def get_account_insights(self) -> dict:
        """Métricas generales de la cuenta (seguidores, alcance)."""
        try:
            data = self._get(f"{self.user_id}/threads_publishing_limit", {
                "fields": "config,quota_usage"
            })
            return data
        except Exception as e:
            log.error(f"[Threads] Error insights cuenta: {e}")
            return {}
