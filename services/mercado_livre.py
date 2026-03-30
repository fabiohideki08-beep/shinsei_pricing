import os
import json
import base64
import hashlib
import secrets
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests

ML_API = "https://api.mercadolibre.com"
ML_AUTH = "https://auth.mercadolivre.com.br/authorization"


class MercadoLivreService:
    def __init__(self, access_token: str):
        self.access_token = access_token

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    def atualizar_preco_standard(self, item_id: str, preco: float):
        url = f"{ML_API}/items/{item_id}"
        payload = {"price": round(preco, 2)}

        response = requests.put(
            url,
            json=payload,
            headers=self._headers(),
            timeout=30
        )

        if response.status_code not in [200, 201]:
            return {"success": False, "status": response.status_code, "error": response.text}

        return {"success": True, "data": response.json()}

    def obter_preco_venda(self, item_id: str):
        url = f"{ML_API}/items/{item_id}"
        response = requests.get(url, headers=self._headers(), timeout=30)

        if response.status_code != 200:
            return {"success": False, "status": response.status_code, "error": response.text}

        return {"success": True, "data": response.json()}

    def atualizar_com_retry(self, item_id: str, preco: float, tentativas: int = 3):
        ultimo = None
        for _ in range(tentativas):
            ultimo = self.atualizar_preco_standard(item_id, preco)
            if ultimo["success"]:
                return ultimo
        return ultimo or {"success": False, "error": "Falha ao atualizar preço no Mercado Livre"}


class MercadoLivreConfigStore:
    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self.data_dir = self.base_dir / "data"
        self.data_dir.mkdir(exist_ok=True)
        self.config_file = self.data_dir / "ml_config.json"

    def load(self):
        if not self.config_file.exists():
            return {
                "client_id": "",
                "client_secret": "",
                "redirect_uri": "",
                "conexao_ativa": False
            }

        try:
            return json.loads(self.config_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save(self, data: dict):
        self.config_file.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        return data


class MercadoLivreOAuthService:
    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self.data_dir = self.base_dir / "data"
        self.data_dir.mkdir(exist_ok=True)

        self.tokens_file = self.data_dir / "ml_tokens.json"
        self.state_file = self.data_dir / "ml_oauth_state.json"

        cfg = MercadoLivreConfigStore(base_dir).load()
        self.client_id = (cfg.get("client_id") or "").strip()
        self.client_secret = (cfg.get("client_secret") or "").strip()
        self.redirect_uri = (cfg.get("redirect_uri") or "").strip()

    def _now_iso(self):
        return datetime.now(timezone.utc).isoformat()

    def _write_json(self, path: Path, payload: dict):
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def _read_json(self, path: Path, default=None):
        if not path.exists():
            return default or {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default or {}

    def validar_config(self):
        faltando = []
        if not self.client_id:
            faltando.append("ML_CLIENT_ID")
        if not self.client_secret:
            faltando.append("ML_CLIENT_SECRET")
        if not self.redirect_uri:
            faltando.append("ML_REDIRECT_URI")
        return faltando

    def gerar_verifier(self) -> str:
        return secrets.token_urlsafe(48).rstrip("=")

    def gerar_challenge(self, verifier: str) -> str:
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def iniciar_login(self):
        faltando = self.validar_config()
        if faltando:
            return {"success": False, "error": f"Configuração ausente: {', '.join(faltando)}"}

        verifier = self.gerar_verifier()
        challenge = self.gerar_challenge(verifier)
        state = secrets.token_urlsafe(24)

        payload = {
            "state": state,
            "code_verifier": verifier,
            "created_at": self._now_iso()
        }
        self._write_json(self.state_file, payload)

        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state
        }

        return {"success": True, "auth_url": f"{ML_AUTH}?{urlencode(params)}"}

    def trocar_code_por_token(self, code: str):
        faltando = self.validar_config()
        if faltando:
            return {"success": False, "error": f"Configuração ausente: {', '.join(faltando)}"}

        oauth_state = self._read_json(self.state_file, {})
        if not oauth_state:
            return {"success": False, "error": "Estado OAuth não encontrado. Inicie novamente em /ml/login."}

        verifier = oauth_state.get("code_verifier")
        if not verifier:
            return {"success": False, "error": "Code verifier não encontrado. Inicie novamente em /ml/login."}

        payload = {
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": verifier
        }

        headers = {
            "accept": "application/json",
            "content-type": "application/x-www-form-urlencoded"
        }

        response = requests.post(
            f"{ML_API}/oauth/token",
            data=payload,
            headers=headers,
            timeout=30
        )

        if response.status_code != 200:
            return {"success": False, "status": response.status_code, "error": response.text}

        data = response.json()
        token_payload = {
            "connected_at": self._now_iso(),
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            **data
        }
        self._write_json(self.tokens_file, token_payload)
        return {"success": True, "data": token_payload}

    def refresh_token(self):
        tokens = self._read_json(self.tokens_file, {})
        refresh_token = tokens.get("refresh_token")

        if not refresh_token:
            return {"success": False, "error": "Refresh token não encontrado"}

        payload = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": refresh_token
        }

        response = requests.post(
            f"{ML_API}/oauth/token",
            data=payload,
            headers={"accept": "application/json"},
            timeout=30
        )

        if response.status_code != 200:
            return {"success": False, "error": response.text}

        data = response.json()
        self._write_json(self.tokens_file, data)
        return {"success": True, "data": data}

    def ler_tokens(self):
        tokens = self._read_json(self.tokens_file, {})
        if not tokens:
            return {"success": False, "error": "Nenhum token salvo"}
        return {"success": True, "data": tokens}

    def status(self):
        cfg = MercadoLivreConfigStore(self.base_dir).load()
        tokens = self._read_json(self.tokens_file, {})

        return {
            "success": True,
            "configurado": bool(cfg.get("client_id") and cfg.get("client_secret") and cfg.get("redirect_uri")),
            "conectado": bool(tokens.get("access_token")),
            "client_id": cfg.get("client_id", ""),
            "redirect_uri": cfg.get("redirect_uri", "")
        }
