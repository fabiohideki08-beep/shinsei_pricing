from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(tags=["regras_precificacao"])

def build_regras_module(data_dir: Path):
    regras_path = data_dir / "regras_precificacao.json"

    default_regras = {
        "regra_ativa": "padrao",
        "perfis": {
            "padrao": {
                "nome": "Padrão",
                "lucro_minimo_auto": 2.0,
                "margem_minima_auto": 20.0,
                "sie_minimo_auto": 0.5,
                "variacao_maxima": 50.0,
                "estoque_minimo_fila": 2,
                "fila_se_preco_atual_zero": True,
                "bloquear_margem_negativa": True,
                "bloquear_lucro_negativo": True
            }
        }
    }

    def load_regras_precificacao() -> dict[str, Any]:
        if not regras_path.exists():
            regras_path.write_text(json.dumps(default_regras, ensure_ascii=False, indent=2), encoding="utf-8")
            return default_regras
        return json.loads(regras_path.read_text(encoding="utf-8"))

    def save_regras_precificacao(data: dict[str, Any]) -> dict[str, Any]:
        regras_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data

    def get_regra_ativa() -> dict[str, Any]:
        data = load_regras_precificacao()
        ativa = data.get("regra_ativa", "padrao")
        perfis = data.get("perfis", {})
        if ativa in perfis:
            return perfis[ativa]
        return perfis.get("padrao", default_regras["perfis"]["padrao"])

    @router.get("/config/regras-precificacao")
    def listar_regras_precificacao():
        return load_regras_precificacao()

    @router.post("/config/regras-precificacao")
    async def salvar_regras_precificacao(request: Request):
        payload = await request.json()
        saved = save_regras_precificacao(payload)
        return {"status": "ok", "config": saved}

    @router.post("/config/regras-precificacao/ativar/{regra_id}")
    def ativar_regra_precificacao(regra_id: str):
        data = load_regras_precificacao()
        perfis = data.get("perfis", {})
        if regra_id not in perfis:
            raise HTTPException(status_code=404, detail="Perfil não encontrado")
        data["regra_ativa"] = regra_id
        save_regras_precificacao(data)
        return {"status": "ok", "regra_ativa": regra_id}

    return load_regras_precificacao, save_regras_precificacao, get_regra_ativa
