
from __future__ import annotations

import base64
import json
import os
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()


class BlingConfigError(RuntimeError):
    pass


class BlingAuthError(RuntimeError):
    pass


class BlingAPIError(RuntimeError):
    pass


class BlingClient:
    AUTH_BASE = "https://www.bling.com.br/Api/v3/oauth"
    API_BASE = "https://api.bling.com.br/Api/v3"
    TOKEN_FILE = Path("bling_token.json")

    def __init__(self) -> None:
        self.client_id = os.getenv("BLING_CLIENT_ID", "").strip()
        self.client_secret = os.getenv("BLING_CLIENT_SECRET", "").strip()
        self.redirect_uri = os.getenv("BLING_REDIRECT_URI", "").strip()

    def _require_config(self) -> None:
        if not self.client_id or not self.client_secret or not self.redirect_uri:
            raise BlingConfigError(
                "Configure BLING_CLIENT_ID, BLING_CLIENT_SECRET e BLING_REDIRECT_URI no .env."
            )

    def has_local_tokens(self) -> bool:
        return self.TOKEN_FILE.exists()

    def build_authorize_url(self) -> str:
        self._require_config()
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "state": secrets.token_urlsafe(16),
        }
        return f"{self.AUTH_BASE}/authorize?{urllib.parse.urlencode(params)}"

    def _basic_auth_header(self) -> str:
        self._require_config()
        raw = f"{self.client_id}:{self.client_secret}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("utf-8")

    def _token_headers(self) -> dict[str, str]:
        return {
            "Authorization": self._basic_auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "enable-jwt": "1",
        }

    def _api_headers(self, access_token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "enable-jwt": "1",
        }

    def _save_token(self, data: dict[str, Any]) -> dict[str, Any]:
        expires_in = int(data.get("expires_in") or 0)
        data["expires_at"] = int(time.time()) + max(expires_in - 60, 0)
        self.TOKEN_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data

    def _load_token(self) -> dict[str, Any]:
        if not self.TOKEN_FILE.exists():
            raise BlingAuthError("Token do Bling não encontrado. Acesse /bling/auth primeiro.")
        return json.loads(self.TOKEN_FILE.read_text(encoding="utf-8"))

    def exchange_code_for_token(self, code: str) -> dict[str, Any]:
        self._require_config()
        body = {"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri}
        resp = requests.post(f"{self.AUTH_BASE}/token", data=body, headers=self._token_headers(), timeout=30)
        try:
            data = resp.json()
        except Exception:
            raise BlingAuthError(f"Falha ao obter token do Bling: HTTP {resp.status_code} {resp.text}")

        if resp.status_code >= 400 or "error" in data:
            if data.get("error") == "invalid_grant" and self.TOKEN_FILE.exists():
                token = self._load_token()
                if token.get("access_token"):
                    return token
            raise BlingAuthError(f"Falha ao obter token do Bling: {data}")
        return self._save_token(data)

    def refresh_access_token(self) -> dict[str, Any]:
        token = self._load_token()
        refresh_token = token.get("refresh_token")
        if not refresh_token:
            raise BlingAuthError("Refresh token não encontrado. Refaça a autenticação em /bling/auth.")
        body = {"grant_type": "refresh_token", "refresh_token": refresh_token}
        resp = requests.post(f"{self.AUTH_BASE}/token", data=body, headers=self._token_headers(), timeout=30)
        try:
            data = resp.json()
        except Exception:
            raise BlingAuthError(f"Falha ao renovar token do Bling: HTTP {resp.status_code} {resp.text}")
        if resp.status_code >= 400 or "error" in data:
            raise BlingAuthError(f"Falha ao renovar token do Bling: {data}")
        return self._save_token(data)

    def get_valid_access_token(self) -> str:
        token = self._load_token()
        if int(token.get("expires_at") or 0) <= int(time.time()):
            token = self.refresh_access_token()
        access_token = token.get("access_token")
        if not access_token:
            raise BlingAuthError("Access token inválido. Refaça a autenticação em /bling/auth.")
        return access_token

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        access_token = self.get_valid_access_token()
        url = f"{self.API_BASE}{path}"
        resp = requests.request(method, url, params=params, json=json_body, headers=self._api_headers(access_token), timeout=30)
        if resp.status_code == 401:
            access_token = self.refresh_access_token()["access_token"]
            resp = requests.request(method, url, params=params, json=json_body, headers=self._api_headers(access_token), timeout=30)
        try:
            data = resp.json()
        except Exception:
            raise BlingAPIError(f"Resposta inválida do Bling: HTTP {resp.status_code} {resp.text}")
        if resp.status_code >= 400:
            raise BlingAPIError(f"Erro na API do Bling: {data}")
        return data

    def search_product(self, criterio: str, valor: str) -> dict[str, Any]:
        valor = (valor or "").strip()
        if not valor:
            raise BlingAPIError("Valor de busca vazio.")

        criterio = (criterio or "").strip().lower()

        if criterio == "id":
            return self.obter_produto_completo(valor)

        params = {"codigo": valor}
        if criterio == "ean":
            params = {"gtin": valor}

        data = self._request("GET", "/produtos", params=params)
        items = data.get("data", [])
        if items:
            item = items[0]
            prod_id = item.get("id")
            if prod_id:
                try:
                    return self.obter_produto_completo(prod_id)
                except Exception:
                    return item
            return item
        raise BlingAPIError("Produto não encontrado")


    def obter_produto_completo(self, produto_id: int | str) -> dict[str, Any]:
        data = self._request("GET", f"/produtos/{produto_id}")
        return data.get("data", data)

    def atualizar_preco(self, produto_id: int, novo_preco: float) -> dict[str, Any]:
        produto = self._request("GET", f"/produtos/{produto_id}")
        data = produto.get("data", produto)
        payload = {
            "nome": data.get("nome") or "",
            "tipo": data.get("tipo") or "P",
            "situacao": data.get("situacao") or "A",
            "formato": data.get("formato") or "S",
            "preco": round(float(novo_preco), 2),
        }
        return self._request("PUT", f"/produtos/{produto_id}", json_body=payload)

    # --------- CAMADA PREMIUM MULTICANAL ---------

    def buscar_anuncios_por_produto(self, produto_id: int) -> dict[str, Any]:
        # endpoint provável conforme a documentação pública listar "Anúncios".
        # caso o seu tenant tenha outra forma de filtrar, ajuste apenas este método.
        return self._request("GET", "/anuncios", params={"idProduto": produto_id})

    def atualizar_anuncio_simples(self, anuncio_id: int | str, preco: float, preco_promocional: float | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"preco": round(float(preco), 2)}
        if preco_promocional is not None:
            payload["precoPromocional"] = round(float(preco_promocional), 2)
        return self._request("PUT", f"/anuncios/{anuncio_id}", json_body=payload)

    def atualizar_anuncio_ml_modalidades(
        self,
        anuncio_id: int | str,
        preco_classico: float,
        preco_premium: float,
    ) -> dict[str, Any]:
        payload = {
            "modalidades": [
                {"tipo": "classico", "preco": round(float(preco_classico), 2)},
                {"tipo": "premium", "preco": round(float(preco_premium), 2)},
            ]
        }
        return self._request("PUT", f"/anuncios/{anuncio_id}", json_body=payload)
