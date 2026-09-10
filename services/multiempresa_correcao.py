# -*- coding: utf-8 -*-
"""
services/multiempresa_correcao.py — Correção de estoque multiempresa venda a venda

Fluxo:
  1. Captura pedidos confirmados da empresa vendedora (pooling ou webhook)
  2. Para cada item: busca rota de 7 níveis (mais específico → mais genérico)
  3. Se rota encontrada: registra saída no fornecedor + entrada/ajuste no vendedor
  4. Se sem rota: marca pendente_configuracao_de_rota (reprocessado depois)
  5. Cancelamento: fluxo inverso simétrico (estorno)

Idempotência: hash SHA1 dos itens da venda; rejeita reprocessamento se status=concluido
              e hash igual. Se itens mudarem, gera versão nova com ajuste diferencial.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH  = BASE_DIR / "data" / "shinsei.db"
BLING_API = "https://api.bling.com.br/Api/v3"

# Empresas suportadas
EMPRESA_SHINSEI = "shinsei"
EMPRESA_AKG     = "akg"

# Depósitos físicos conhecidos
DEP_SHINSEI_GERAL = 14636070822
DEP_AKG_GERAL     = 14889056234

# Situações Bling que consideramos "venda confirmada" para disparar ajuste
SITUACOES_CONFIRMADAS = {9, 12, 15}   # Em andamento, Faturado, etc.
# Situações que disparam cancelamento/estorno
SITUACOES_CANCELADAS  = {11, 14, 76}  # Cancelado, Devolvido, etc.


# ─────────────────────────────────────────────────────────────────────────────
# Banco de dados
# ─────────────────────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """Cria/migra tabelas do módulo multiempresa."""
    conn = _db()
    conn.executescript("""
    -- Tabela de rotas de estoque (núcleo administrável)
    CREATE TABLE IF NOT EXISTS me_rotas (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        sku                     TEXT,               -- NULL = qualquer SKU
        empresa_vendedora       TEXT,               -- NULL = qualquer
        canal_venda             TEXT,               -- NULL = qualquer
        deposito_venda          TEXT,               -- NULL = qualquer
        empresa_fornecedora     TEXT    NOT NULL,
        deposito_fornecedor_id  INTEGER NOT NULL,
        deposito_fornecedor_nome TEXT,
        ativo                   INTEGER DEFAULT 1,
        vigencia_inicio         TEXT,               -- YYYY-MM-DD; NULL = sem limite
        vigencia_fim            TEXT,               -- YYYY-MM-DD; NULL = sem limite
        obs                     TEXT,
        criado_em               TEXT    NOT NULL,
        atualizado_em           TEXT
    );

    -- Controle de vendas processadas (idempotência por venda)
    CREATE TABLE IF NOT EXISTS me_vendas (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        id_pedido_bling     TEXT    NOT NULL,
        empresa_vendedora   TEXT    NOT NULL,
        hash_itens          TEXT    NOT NULL,   -- SHA1 da lista ordenada de itens
        status              TEXT    NOT NULL,
        -- pendente | processando | concluido | erro | cancelado | estornado
        total_itens         INTEGER DEFAULT 0,
        itens_ok            INTEGER DEFAULT 0,
        itens_erro          INTEGER DEFAULT 0,
        itens_sem_rota      INTEGER DEFAULT 0,
        data_venda          TEXT,
        criado_em           TEXT    NOT NULL,
        atualizado_em       TEXT,
        UNIQUE(id_pedido_bling, empresa_vendedora, hash_itens)
    );

    -- Ajustes individuais por item/venda
    CREATE TABLE IF NOT EXISTS me_ajustes (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        id_venda_ctrl           INTEGER REFERENCES me_vendas(id),
        id_pedido_bling         TEXT    NOT NULL,
        empresa_vendedora       TEXT    NOT NULL,
        canal_venda             TEXT,
        deposito_venda          TEXT,
        sku                     TEXT    NOT NULL,
        id_item_bling           TEXT,
        quantidade              REAL    NOT NULL,
        id_rota                 INTEGER REFERENCES me_rotas(id),
        empresa_fornecedora     TEXT,
        deposito_fornecedor_id  INTEGER,
        status                  TEXT    NOT NULL,
        -- pendente_configuracao_de_rota | processando | concluido | erro
        id_mov_saida_bling      TEXT,   -- ID movimentação saída no fornecedor
        id_mov_entrada_bling    TEXT,   -- ID movimentação entrada no vendedor
        chave_idempotencia      TEXT,   -- "VENDA:<id>-SKU:<sku>" gravada em obs
        erro_detalhe            TEXT,
        criado_em               TEXT    NOT NULL,
        aplicado_em             TEXT,
        UNIQUE(id_pedido_bling, empresa_vendedora, sku)
    );

    -- Estornos (cancelamentos e devoluções)
    CREATE TABLE IF NOT EXISTS me_estornos (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        id_ajuste           INTEGER REFERENCES me_ajustes(id),
        id_pedido_bling     TEXT    NOT NULL,
        sku                 TEXT    NOT NULL,
        quantidade          REAL    NOT NULL,
        status              TEXT    NOT NULL,   -- pendente | concluido | erro
        id_mov_entrada_forn TEXT,
        id_mov_saida_vend   TEXT,
        erro_detalhe        TEXT,
        criado_em           TEXT    NOT NULL,
        aplicado_em         TEXT,
        UNIQUE(id_ajuste)
    );

    CREATE INDEX IF NOT EXISTS ix_me_ajustes_status    ON me_ajustes(status);
    CREATE INDEX IF NOT EXISTS ix_me_ajustes_pedido    ON me_ajustes(id_pedido_bling);
    CREATE INDEX IF NOT EXISTS ix_me_vendas_status     ON me_vendas(status);
    CREATE INDEX IF NOT EXISTS ix_me_rotas_sku         ON me_rotas(sku);
    """)
    conn.commit()
    conn.close()
    logger.info("me_*: tabelas inicializadas")


# ─────────────────────────────────────────────────────────────────────────────
# Autenticação Bling por empresa
# ─────────────────────────────────────────────────────────────────────────────

def _hdrs(empresa: str) -> dict:
    if empresa == EMPRESA_SHINSEI:
        from bling_client import BlingClient
        return BlingClient()._get_headers()
    elif empresa == EMPRESA_AKG:
        import importlib, sys
        app = sys.modules.get("app") or importlib.import_module("app")
        return app._bling_akg_headers()
    raise ValueError(f"Empresa desconhecida: {empresa}")


# ─────────────────────────────────────────────────────────────────────────────
# Extração de contexto do pedido Bling
# ─────────────────────────────────────────────────────────────────────────────

# Mapeamento de CNPJ dos intermediadores para nome de canal
_CNPJ_CANAL = {
    "03.007.331": "MERCADO_LIVRE",
    "38.098.442": "SHOPEE",
    "15.436.940": "SHOPIFY",
    "24.269.250": "AMAZON",
}


def extrair_canal(pedido: dict) -> str | None:
    """
    Deriva o canal de venda a partir dos dados do pedido Bling.
    Prioriza nomeUsuario (específico por conta) depois CNPJ do intermediador.
    """
    inter = pedido.get("intermediador") or {}
    nome = (inter.get("nomeUsuario") or "").strip().upper()
    if nome:
        return nome  # ex: "AKG_OFICIAL", "SHINSEI_HACHI"

    cnpj = (inter.get("cnpj") or "").strip()
    for prefixo, canal in _CNPJ_CANAL.items():
        if cnpj.startswith(prefixo):
            return canal

    loja = pedido.get("loja") or {}
    tipo = (loja.get("tipo") or "").upper()
    return tipo or None


def _hash_itens(itens: list[dict]) -> str:
    """SHA1 dos itens ordenados por SKU — base da idempotência."""
    chave = json.dumps(
        sorted(
            [{"sku": str(i.get("codigo") or ""), "qtd": float(i.get("quantidade") or 0)}
             for i in itens],
            key=lambda x: x["sku"]
        ),
        ensure_ascii=False
    )
    return hashlib.sha1(chave.encode()).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# Roteamento — 7 níveis de fallback
# ─────────────────────────────────────────────────────────────────────────────

_ROTA_SQL = """
SELECT * FROM me_rotas
WHERE ativo = 1
  AND (vigencia_inicio IS NULL OR vigencia_inicio <= :data)
  AND (vigencia_fim    IS NULL OR vigencia_fim    >= :data)
  AND (
      (sku = :sku AND empresa_vendedora = :emp AND canal_venda = :canal AND deposito_venda = :dep)
   OR (sku = :sku AND empresa_vendedora = :emp AND canal_venda = :canal AND deposito_venda IS NULL)
   OR (sku = :sku AND empresa_vendedora = :emp AND canal_venda IS NULL  AND deposito_venda = :dep)
   OR (sku = :sku AND empresa_vendedora = :emp AND canal_venda IS NULL  AND deposito_venda IS NULL)
   OR (sku = :sku AND empresa_vendedora IS NULL AND canal_venda IS NULL AND deposito_venda IS NULL)
   OR (sku IS NULL AND empresa_vendedora = :emp AND canal_venda IS NULL AND deposito_venda IS NULL)
  )
ORDER BY
  (sku IS NOT NULL)              DESC,
  (empresa_vendedora IS NOT NULL) DESC,
  (canal_venda IS NOT NULL)      DESC,
  (deposito_venda IS NOT NULL)   DESC
LIMIT 1
"""


def buscar_rota(conn: sqlite3.Connection, sku: str, empresa_vendedora: str,
                canal: str | None, deposito: str | None,
                data: str) -> sqlite3.Row | None:
    return conn.execute(_ROTA_SQL, {
        "sku": sku, "emp": empresa_vendedora,
        "canal": canal, "dep": deposito, "data": data
    }).fetchone()


# ─────────────────────────────────────────────────────────────────────────────
# Movimentações de estoque Bling
# ─────────────────────────────────────────────────────────────────────────────

def _buscar_id_produto(empresa: str, sku: str) -> int | None:
    hdrs = _hdrs(empresa)
    resp = requests.get(f"{BLING_API}/produtos?codigo={sku}&limite=5",
                        headers=hdrs, timeout=15)
    if not resp.ok:
        return None
    for item in resp.json().get("data", []):
        if str(item.get("codigo") or "") == sku:
            return item["id"]
    return None


def _movimentar(empresa: str, id_produto: int, deposito_id: int,
                operacao: str, quantidade: float, obs: str) -> str | None:
    """
    POST /estoques/movimentacoes.
    operacao: 'E' (entrada) ou 'S' (saída).
    Retorna ID da movimentação criada ou None em caso de erro.
    """
    hdrs = _hdrs(empresa)
    payload = {
        "produto":    {"id": id_produto},
        "deposito":   {"id": deposito_id},
        "operacao":   operacao,
        "quantidade": quantidade,
        "observacoes": obs[:250],
    }
    resp = requests.post(
        f"{BLING_API}/estoques/movimentacoes",
        headers={**hdrs, "Content-Type": "application/json"},
        json=payload, timeout=20
    )
    if not resp.ok:
        logger.error("movimentar [%s/%s op=%s] HTTP %s: %s",
                     empresa, id_produto, operacao, resp.status_code, resp.text[:300])
        return None
    data = resp.json().get("data") or {}
    return str(data.get("id") or "ok")


# ─────────────────────────────────────────────────────────────────────────────
# Processamento de um pedido
# ─────────────────────────────────────────────────────────────────────────────

def _agora() -> str:
    return datetime.now(timezone.utc).isoformat()


def processar_pedido(empresa_vendedora: str, pedido: dict) -> dict:
    """
    Processa um pedido completo venda a venda.
    Retorna resumo {id_pedido, status, itens: [...]}.
    """
    init_db()
    conn = _db()

    id_pedido  = str(pedido.get("id") or "")
    itens      = pedido.get("itens") or []
    canal      = extrair_canal(pedido)
    data_venda = (pedido.get("data") or _agora()[:10])[:10]
    hash_it    = _hash_itens(itens)

    # ── Registro de controle de venda (idempotência) ──────────────────────────
    ctrl = conn.execute(
        "SELECT * FROM me_vendas WHERE id_pedido_bling=? AND empresa_vendedora=?",
        (id_pedido, empresa_vendedora)
    ).fetchone()

    if ctrl and ctrl["status"] == "concluido" and ctrl["hash_itens"] == hash_it:
        conn.close()
        return {"id_pedido": id_pedido, "status": "duplicata_ignorada", "itens": []}

    agora = _agora()
    if ctrl:
        # Nova versão com itens diferentes ou reprocessamento de erro
        conn.execute(
            "UPDATE me_vendas SET hash_itens=?, status='processando', atualizado_em=? "
            "WHERE id_pedido_bling=? AND empresa_vendedora=?",
            (hash_it, agora, id_pedido, empresa_vendedora)
        )
        id_ctrl = ctrl["id"]
    else:
        cur = conn.execute(
            """INSERT INTO me_vendas
               (id_pedido_bling, empresa_vendedora, hash_itens, status,
                total_itens, data_venda, criado_em, atualizado_em)
               VALUES (?,?,?,?,?,?,?,?)""",
            (id_pedido, empresa_vendedora, hash_it, "processando",
             len(itens), data_venda, agora, agora)
        )
        id_ctrl = cur.lastrowid
    conn.commit()

    resultados = []
    n_ok = n_erro = n_sem_rota = 0

    for item in itens:
        sku      = str(item.get("codigo") or "").strip()
        qtd      = float(item.get("quantidade") or 0)
        id_item  = str(item.get("id") or "")
        dep_venda = str(item.get("deposito", {}).get("id") or "") or None

        if not sku or qtd <= 0:
            continue

        chave = f"VENDA:{id_pedido}-SKU:{sku}"

        # Idempotência por item
        ajuste_existente = conn.execute(
            "SELECT * FROM me_ajustes "
            "WHERE id_pedido_bling=? AND empresa_vendedora=? AND sku=?",
            (id_pedido, empresa_vendedora, sku)
        ).fetchone()

        if ajuste_existente and ajuste_existente["status"] == "concluido":
            resultados.append({"sku": sku, "status": "duplicata_ignorada"})
            n_ok += 1
            continue

        # Passo 2: roteamento
        rota = buscar_rota(conn, sku, empresa_vendedora, canal, dep_venda, data_venda)

        if not rota:
            status_item = "pendente_configuracao_de_rota"
            n_sem_rota += 1
            if not ajuste_existente:
                conn.execute(
                    """INSERT OR IGNORE INTO me_ajustes
                       (id_venda_ctrl, id_pedido_bling, empresa_vendedora,
                        canal_venda, deposito_venda, sku, id_item_bling,
                        quantidade, status, chave_idempotencia, criado_em)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (id_ctrl, id_pedido, empresa_vendedora,
                     canal, dep_venda, sku, id_item,
                     qtd, status_item, chave, _agora())
                )
            resultados.append({"sku": sku, "status": status_item})
            conn.commit()
            continue

        emp_forn  = rota["empresa_fornecedora"]
        dep_forn  = rota["deposito_fornecedor_id"]
        dep_vend_id = DEP_SHINSEI_GERAL if empresa_vendedora == EMPRESA_SHINSEI else DEP_AKG_GERAL

        # Buscar IDs dos produtos em cada empresa
        id_prod_forn = _buscar_id_produto(emp_forn, sku)
        time.sleep(0.15)
        id_prod_vend = _buscar_id_produto(empresa_vendedora, sku) if emp_forn != empresa_vendedora else id_prod_forn
        time.sleep(0.15)

        if not id_prod_forn:
            status_item = "erro"
            n_erro += 1
            conn.execute(
                """INSERT OR REPLACE INTO me_ajustes
                   (id_venda_ctrl, id_pedido_bling, empresa_vendedora,
                    canal_venda, deposito_venda, sku, id_item_bling,
                    quantidade, id_rota, empresa_fornecedora, deposito_fornecedor_id,
                    status, chave_idempotencia, erro_detalhe, criado_em)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (id_ctrl, id_pedido, empresa_vendedora,
                 canal, dep_venda, sku, id_item,
                 qtd, rota["id"], emp_forn, dep_forn,
                 status_item, chave, f"produto nao encontrado na empresa {emp_forn}", _agora())
            )
            conn.commit()
            resultados.append({"sku": sku, "status": status_item,
                                "erro": f"SKU não encontrado em {emp_forn}"})
            continue

        # Passo 3: saída no fornecedor
        obs_saida = f"{chave}|etapa=saida|forn={emp_forn}"
        id_mov_saida = _movimentar(emp_forn, id_prod_forn, dep_forn, "S", qtd, obs_saida)
        time.sleep(0.35)

        # Passo 4: entrada/ajuste no vendedor (se empresa diferente)
        id_mov_entrada = None
        if emp_forn != empresa_vendedora and id_prod_vend:
            obs_entrada = f"{chave}|etapa=entrada|vend={empresa_vendedora}"
            id_mov_entrada = _movimentar(empresa_vendedora, id_prod_vend, dep_vend_id, "E", qtd, obs_entrada)
            time.sleep(0.35)

        status_item = "concluido" if id_mov_saida else "erro"
        if status_item == "concluido":
            n_ok += 1
        else:
            n_erro += 1

        conn.execute(
            """INSERT OR REPLACE INTO me_ajustes
               (id_venda_ctrl, id_pedido_bling, empresa_vendedora,
                canal_venda, deposito_venda, sku, id_item_bling,
                quantidade, id_rota, empresa_fornecedora, deposito_fornecedor_id,
                status, id_mov_saida_bling, id_mov_entrada_bling,
                chave_idempotencia, criado_em, aplicado_em)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (id_ctrl, id_pedido, empresa_vendedora,
             canal, dep_venda, sku, id_item,
             qtd, rota["id"], emp_forn, dep_forn,
             status_item, id_mov_saida, id_mov_entrada,
             chave, _agora(), _agora() if status_item == "concluido" else None)
        )
        conn.commit()
        resultados.append({
            "sku": sku, "qtd": qtd, "status": status_item,
            "fornecedor": emp_forn, "dep_forn": dep_forn,
            "mov_saida": id_mov_saida, "mov_entrada": id_mov_entrada,
        })

    # Atualiza controle
    status_ctrl = (
        "concluido" if n_erro == 0 and n_sem_rota == 0
        else "erro"   if n_ok == 0
        else "concluido_parcial"
    )
    conn.execute(
        """UPDATE me_vendas SET status=?, itens_ok=?, itens_erro=?,
           itens_sem_rota=?, atualizado_em=? WHERE id=?""",
        (status_ctrl, n_ok, n_erro, n_sem_rota, _agora(), id_ctrl)
    )
    conn.commit()
    conn.close()

    return {
        "id_pedido": id_pedido, "status": status_ctrl,
        "ok": n_ok, "erro": n_erro, "sem_rota": n_sem_rota,
        "itens": resultados,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Cancelamento / estorno
# ─────────────────────────────────────────────────────────────────────────────

def estornar_pedido(empresa_vendedora: str, id_pedido: str) -> dict:
    """
    Fluxo inverso: para cada ajuste concluído do pedido,
    gera entrada no fornecedor + saída no vendedor.
    """
    init_db()
    conn = _db()

    ajustes = conn.execute(
        "SELECT * FROM me_ajustes "
        "WHERE id_pedido_bling=? AND empresa_vendedora=? AND status='concluido'",
        (id_pedido, empresa_vendedora)
    ).fetchall()

    if not ajustes:
        conn.close()
        return {"id_pedido": id_pedido, "status": "sem_ajustes_para_estornar", "itens": []}

    resultados = []
    for aj in ajustes:
        # Idempotência de estorno
        ja_estornado = conn.execute(
            "SELECT status FROM me_estornos WHERE id_ajuste=?", (aj["id"],)
        ).fetchone()
        if ja_estornado and ja_estornado["status"] == "concluido":
            resultados.append({"sku": aj["sku"], "status": "ja_estornado"})
            continue

        sku      = aj["sku"]
        qtd      = aj["quantidade"]
        emp_forn = aj["empresa_fornecedora"]
        dep_forn = aj["deposito_fornecedor_id"]
        dep_vend = DEP_SHINSEI_GERAL if empresa_vendedora == EMPRESA_SHINSEI else DEP_AKG_GERAL
        chave    = f"ESTORNO:{id_pedido}-SKU:{sku}"

        id_prod_forn = _buscar_id_produto(emp_forn, sku)
        id_prod_vend = _buscar_id_produto(empresa_vendedora, sku) if emp_forn != empresa_vendedora else id_prod_forn
        time.sleep(0.2)

        # Inverso passo 3: entrada no fornecedor
        id_mov_ef = None
        if id_prod_forn:
            id_mov_ef = _movimentar(emp_forn, id_prod_forn, dep_forn, "E", qtd,
                                    f"{chave}|etapa=estorno_saida_forn")
            time.sleep(0.35)

        # Inverso passo 4: saída no vendedor
        id_mov_sv = None
        if emp_forn != empresa_vendedora and id_prod_vend:
            id_mov_sv = _movimentar(empresa_vendedora, id_prod_vend, dep_vend, "S", qtd,
                                    f"{chave}|etapa=estorno_entrada_vend")
            time.sleep(0.35)

        status_est = "concluido" if id_mov_ef else "erro"
        conn.execute(
            """INSERT OR REPLACE INTO me_estornos
               (id_ajuste, id_pedido_bling, sku, quantidade, status,
                id_mov_entrada_forn, id_mov_saida_vend, criado_em, aplicado_em)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (aj["id"], id_pedido, sku, qtd, status_est,
             id_mov_ef, id_mov_sv, _agora(),
             _agora() if status_est == "concluido" else None)
        )
        conn.commit()
        resultados.append({"sku": sku, "status": status_est,
                            "mov_entrada_forn": id_mov_ef, "mov_saida_vend": id_mov_sv})

    # Marca a venda como estornada se todos OK
    todos_ok = all(r["status"] == "concluido" for r in resultados)
    conn.execute(
        "UPDATE me_vendas SET status=?, atualizado_em=? "
        "WHERE id_pedido_bling=? AND empresa_vendedora=?",
        ("estornada" if todos_ok else "estorno_parcial",
         _agora(), id_pedido, empresa_vendedora)
    )
    conn.commit()
    conn.close()

    return {
        "id_pedido": id_pedido,
        "status": "estornada" if todos_ok else "estorno_parcial",
        "itens": resultados,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Job periódico
# ─────────────────────────────────────────────────────────────────────────────

_ultima_verificacao: dict[str, str] = {}


def job_multiempresa():
    """
    Verifica pedidos recentes de Shinsei e AKG:
    - Novos confirmados → processar_pedido
    - Cancelados com ajuste → estornar_pedido
    - Pendentes de rota → tentar reprocessar
    Chamado pelo scheduler a cada ~10 min.
    """
    init_db()
    hoje = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for empresa in [EMPRESA_SHINSEI, EMPRESA_AKG]:
        data_desde = _ultima_verificacao.get(empresa, hoje)
        hdrs = _hdrs(empresa)

        try:
            # Vendas confirmadas
            params = f"pagina=1&limite=100&dataInicial={data_desde}"
            resp = requests.get(f"{BLING_API}/pedidos/vendas?{params}",
                                headers=hdrs, timeout=20)
            if not resp.ok:
                logger.warning("job_me [%s] HTTP %s", empresa, resp.status_code)
                continue

            pedidos = resp.json().get("data", [])
            logger.info("job_multiempresa [%s]: %d pedidos desde %s", empresa, len(pedidos), data_desde)

            for pedido in pedidos:
                sit = int((pedido.get("situacao") or {}).get("id") or 0)
                id_ped = str(pedido.get("id") or "")

                if sit in SITUACOES_CONFIRMADAS:
                    processar_pedido(empresa, pedido)
                elif sit in SITUACOES_CANCELADAS:
                    estornar_pedido(empresa, id_ped)

                time.sleep(0.3)

            # Reprocessar itens sem rota (alguém pode ter cadastrado rota nova)
            _reprocessar_sem_rota(empresa)
            _ultima_verificacao[empresa] = hoje

        except Exception as e:
            logger.error("job_multiempresa [%s]: %s", empresa, e)


def _reprocessar_sem_rota(empresa_vendedora: str):
    """Tenta reprocessar ajustes que ficaram pendentes por falta de rota."""
    conn = _db()
    pendentes = conn.execute(
        "SELECT DISTINCT id_pedido_bling FROM me_ajustes "
        "WHERE empresa_vendedora=? AND status='pendente_configuracao_de_rota'",
        (empresa_vendedora,)
    ).fetchall()
    conn.close()

    for row in pendentes:
        id_ped = row["id_pedido_bling"]
        hdrs = _hdrs(empresa_vendedora)
        try:
            resp = requests.get(f"{BLING_API}/pedidos/vendas/{id_ped}",
                                headers=hdrs, timeout=15)
            if resp.ok:
                pedido = resp.json().get("data") or {}
                if pedido:
                    processar_pedido(empresa_vendedora, pedido)
            time.sleep(0.5)
        except Exception as e:
            logger.warning("reprocessar [%s/%s]: %s", empresa_vendedora, id_ped, e)


# ─────────────────────────────────────────────────────────────────────────────
# CRUD de rotas (usado pela rota FastAPI)
# ─────────────────────────────────────────────────────────────────────────────

def listar_rotas(apenas_ativas: bool = True) -> list[dict]:
    conn = _db()
    where = "WHERE ativo=1" if apenas_ativas else ""
    rows = conn.execute(f"SELECT * FROM me_rotas {where} ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def criar_rota(dados: dict) -> int:
    conn = _db()
    cur = conn.execute(
        """INSERT INTO me_rotas
           (sku, empresa_vendedora, canal_venda, deposito_venda,
            empresa_fornecedora, deposito_fornecedor_id, deposito_fornecedor_nome,
            ativo, vigencia_inicio, vigencia_fim, obs, criado_em)
           VALUES (:sku,:empresa_vendedora,:canal_venda,:deposito_venda,
                   :empresa_fornecedora,:deposito_fornecedor_id,:deposito_fornecedor_nome,
                   :ativo,:vigencia_inicio,:vigencia_fim,:obs,:criado_em)""",
        {
            "sku": dados.get("sku"),
            "empresa_vendedora": dados.get("empresa_vendedora"),
            "canal_venda": dados.get("canal_venda"),
            "deposito_venda": dados.get("deposito_venda"),
            "empresa_fornecedora": dados["empresa_fornecedora"],
            "deposito_fornecedor_id": dados["deposito_fornecedor_id"],
            "deposito_fornecedor_nome": dados.get("deposito_fornecedor_nome"),
            "ativo": dados.get("ativo", 1),
            "vigencia_inicio": dados.get("vigencia_inicio"),
            "vigencia_fim": dados.get("vigencia_fim"),
            "obs": dados.get("obs"),
            "criado_em": _agora(),
        }
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def atualizar_rota(id_rota: int, dados: dict):
    conn = _db()
    campos = {k: v for k, v in dados.items()
              if k in ("sku","empresa_vendedora","canal_venda","deposito_venda",
                       "empresa_fornecedora","deposito_fornecedor_id",
                       "deposito_fornecedor_nome","ativo","vigencia_inicio","vigencia_fim","obs")}
    campos["atualizado_em"] = _agora()
    sets = ", ".join(f"{k}=:{k}" for k in campos)
    campos["id"] = id_rota
    conn.execute(f"UPDATE me_rotas SET {sets} WHERE id=:id", campos)
    conn.commit()
    conn.close()


def desativar_rota(id_rota: int):
    atualizar_rota(id_rota, {"ativo": 0})
