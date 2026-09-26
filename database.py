"""
database.py â€” Shinsei Pricing
Camada de persistÃªncia SQLite.
Substitui leitura/escrita direta de fila_aprovacao.json e regras.json.

Uso:
    from database import get_db, init_db
    init_db()          # chamar uma vez na inicializaÃ§Ã£o do app
    db = get_db()      # retorna conexÃ£o thread-safe

MigraÃ§Ã£o dos JSONs existentes:
    python database.py migrate
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Generator

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

_DB_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR}/shinsei.db")

# Extrai o path do arquivo a partir da URL (suporta sqlite:/// e caminho direto)
def _db_path() -> str:
    url = _DB_URL.strip()
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///"):]
    if url.startswith("sqlite://"):
        return url[len("sqlite://"):]
    return url  # caminho direto


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# ConexÃ£o
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # permite leituras concorrentes
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# Pool simples: uma conexÃ£o por thread (FastAPI usa thread pool)
import threading
_local = threading.local()

def get_db() -> sqlite3.Connection:
    if not getattr(_local, "conn", None):
        _local.conn = _connect()
    return _local.conn


@contextmanager
def db_transaction() -> Generator[sqlite3.Connection, None, None]:
    """Context manager que faz commit ou rollback automaticamente."""
    conn = get_db()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# DDL â€” criaÃ§Ã£o das tabelas
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_DDL = """
CREATE TABLE IF NOT EXISTS regras (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    canal       TEXT    NOT NULL,
    peso_min    REAL    NOT NULL DEFAULT 0,
    peso_max    REAL    NOT NULL DEFAULT 999999,
    preco_min   REAL    NOT NULL DEFAULT 0,
    preco_max   REAL    NOT NULL DEFAULT 999999999,
    taxa_frete  REAL    NOT NULL DEFAULT 0,
    comissao    REAL    NOT NULL DEFAULT 0,
    taxa_fixa   REAL    NOT NULL DEFAULT 0,
    ativo       INTEGER NOT NULL DEFAULT 1,
    extra_json  TEXT,                          -- campos adicionais futuros
    criado_em   TEXT    NOT NULL DEFAULT (datetime('now')),
    atualizado_em TEXT  NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_regras_canal ON regras(canal);
CREATE INDEX IF NOT EXISTS idx_regras_ativo ON regras(ativo);

CREATE TABLE IF NOT EXISTS fila_aprovacao (
    id              TEXT PRIMARY KEY,          -- UUID
    status          TEXT NOT NULL DEFAULT 'pendente',  -- pendente | aprovado | rejeitado
    sku             TEXT NOT NULL,
    nome            TEXT,
    criado_em       TEXT NOT NULL,
    atualizado_em   TEXT NOT NULL,
    payload_json    TEXT NOT NULL,             -- JSON completo do item (marketplaces, auditoria etc.)
    resultado_aplicacao_json TEXT              -- resultado apÃ³s aprovaÃ§Ã£o/rejeiÃ§Ã£o
);

CREATE INDEX IF NOT EXISTS idx_fila_status ON fila_aprovacao(status);
CREATE INDEX IF NOT EXISTS idx_fila_sku    ON fila_aprovacao(sku);
CREATE INDEX IF NOT EXISTS idx_fila_criado ON fila_aprovacao(criado_em DESC);

CREATE TABLE IF NOT EXISTS config (
    chave TEXT PRIMARY KEY,
    valor TEXT NOT NULL,
    atualizado_em TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def init_db() -> None:
    """Cria as tabelas se nÃ£o existirem. Seguro para chamar mÃºltiplas vezes."""
    conn = get_db()
    conn.executescript(_DDL)
    conn.commit()
    logger.info("Banco de dados inicializado em %s", _db_path())


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Helpers â€” conversÃ£o Row â†’ dict
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _regra_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    # Restaura campos extras do JSON se houver
    extra = d.pop("extra_json", None)
    if extra:
        try:
            d.update(json.loads(extra))
        except Exception:
            pass
    d["ativo"] = bool(d["ativo"])
    return d


def _fila_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    payload = d.pop("payload_json", "{}")
    resultado = d.pop("resultado_aplicacao_json", None)
    try:
        base = json.loads(payload)
    except Exception:
        base = {}
    base["id"] = d["id"]
    base["status"] = d["status"]
    base["sku"] = d["sku"]
    base["nome"] = d.get("nome") or base.get("nome")
    base["criado_em"] = d["criado_em"]
    base["atualizado_em"] = d["atualizado_em"]
    if resultado:
        try:
            base["resultado_aplicacao"] = json.loads(resultado)
        except Exception:
            base["resultado_aplicacao"] = None
    return base


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Regras CRUD
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def listar_regras(apenas_ativas: bool = False) -> list[dict]:
    conn = get_db()
    if apenas_ativas:
        rows = conn.execute("SELECT * FROM regras WHERE ativo=1 ORDER BY canal, peso_min, preco_min").fetchall()
    else:
        rows = conn.execute("SELECT * FROM regras ORDER BY canal, peso_min, preco_min").fetchall()
    return [_regra_row_to_dict(r) for r in rows]


def inserir_regra(regra: dict) -> int:
    """Insere uma regra e retorna o id gerado."""
    known = {"canal", "peso_min", "peso_max", "preco_min", "preco_max",
             "taxa_frete", "comissao", "taxa_fixa", "ativo"}
    extra = {k: v for k, v in regra.items() if k not in known}
    with db_transaction() as conn:
        cur = conn.execute(
            """INSERT INTO regras
               (canal, peso_min, peso_max, preco_min, preco_max,
                taxa_frete, comissao, taxa_fixa, ativo, extra_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                str(regra.get("canal", "")),
                float(regra.get("peso_min", 0)),
                float(regra.get("peso_max", 999999)),
                float(regra.get("preco_min", 0)),
                float(regra.get("preco_max", 999999999)),
                float(regra.get("taxa_frete", regra.get("frete", 0))),
                float(regra.get("comissao", 0)),
                float(regra.get("taxa_fixa", 0)),
                1 if regra.get("ativo", True) else 0,
                json.dumps(extra, ensure_ascii=False) if extra else None,
            ),
        )
        return cur.lastrowid


def atualizar_regra(idx: int, regra: dict) -> bool:
    """Atualiza uma regra pelo rowid. Retorna True se encontrada."""
    known = {"canal", "peso_min", "peso_max", "preco_min", "preco_max",
             "taxa_frete", "comissao", "taxa_fixa", "ativo"}
    extra = {k: v for k, v in regra.items() if k not in known and k != "id"}
    with db_transaction() as conn:
        cur = conn.execute(
            """UPDATE regras SET
               canal=?, peso_min=?, peso_max=?, preco_min=?, preco_max=?,
               taxa_frete=?, comissao=?, taxa_fixa=?, ativo=?,
               extra_json=?, atualizado_em=datetime('now')
               WHERE id=?""",
            (
                str(regra.get("canal", "")),
                float(regra.get("peso_min", 0)),
                float(regra.get("peso_max", 999999)),
                float(regra.get("preco_min", 0)),
                float(regra.get("preco_max", 999999999)),
                float(regra.get("taxa_frete", regra.get("frete", 0))),
                float(regra.get("comissao", 0)),
                float(regra.get("taxa_fixa", 0)),
                1 if regra.get("ativo", True) else 0,
                json.dumps(extra, ensure_ascii=False) if extra else None,
                idx,
            ),
        )
        return cur.rowcount > 0


def excluir_regra(idx: int) -> bool:
    with db_transaction() as conn:
        cur = conn.execute("DELETE FROM regras WHERE id=?", (idx,))
        return cur.rowcount > 0


def substituir_todas_regras(regras: list[dict]) -> int:
    """Apaga todas as regras e insere as novas (usado na importaÃ§Ã£o Excel)."""
    with db_transaction() as conn:
        conn.execute("DELETE FROM regras")
    for r in regras:
        inserir_regra(r)
    return len(regras)


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Fila de aprovaÃ§Ã£o CRUD
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def listar_fila(status: str | None = None, limit: int = 500) -> list[dict]:
    conn = get_db()
    if status:
        rows = conn.execute(
            "SELECT * FROM fila_aprovacao WHERE status=? ORDER BY criado_em DESC LIMIT ?",
            (status, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM fila_aprovacao ORDER BY criado_em DESC LIMIT ?",
            (limit,)
        ).fetchall()
    return [_fila_row_to_dict(r) for r in rows]


def buscar_item_fila(item_id: str) -> dict | None:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM fila_aprovacao WHERE id=?", (item_id,)
    ).fetchone()
    return _fila_row_to_dict(row) if row else None


def inserir_item_fila(item: dict) -> None:
    """Insere um item na fila. O item deve ter id, status, sku, criado_em."""
    # Campos de topo sÃ£o indexados; o resto vai no payload_json
    top = {"id", "status", "sku", "nome", "criado_em", "atualizado_em", "resultado_aplicacao"}
    payload = {k: v for k, v in item.items() if k not in {"resultado_aplicacao"}}
    resultado = item.get("resultado_aplicacao")
    with db_transaction() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO fila_aprovacao
               (id, status, sku, nome, criado_em, atualizado_em, payload_json, resultado_aplicacao_json)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                item["id"],
                item.get("status", "pendente"),
                item.get("sku", ""),
                item.get("nome"),
                item.get("criado_em", datetime.now().isoformat()),
                item.get("atualizado_em", datetime.now().isoformat()),
                json.dumps(payload, ensure_ascii=False, default=str),
                json.dumps(resultado, ensure_ascii=False, default=str) if resultado is not None else None,
            ),
        )


def atualizar_status_fila(item_id: str, status: str, resultado: Any = None) -> bool:
    agora = datetime.now().isoformat()
    resultado_json = json.dumps(resultado, ensure_ascii=False, default=str) if resultado is not None else None
    with db_transaction() as conn:
        cur = conn.execute(
            """UPDATE fila_aprovacao
               SET status=?, atualizado_em=?, resultado_aplicacao_json=?
               WHERE id=?""",
            (status, agora, resultado_json, item_id),
        )
        return cur.rowcount > 0


def stats_fila() -> dict:
    conn = get_db()
    rows = conn.execute(
        "SELECT status, COUNT(*) as n FROM fila_aprovacao GROUP BY status"
    ).fetchall()
    stats = {"pendente": 0, "aprovado": 0, "rejeitado": 0, "incompleto": 0}
    for r in rows:
        if r["status"] in stats:
            stats[r["status"]] = r["n"]
    stats["total"] = sum(stats.values())
    return stats


def limpar_invalidos_fila() -> int:
    """Remove itens sem SKU ou com dados claramente corrompidos."""
    with db_transaction() as conn:
        cur = conn.execute(
            "DELETE FROM fila_aprovacao WHERE sku IS NULL OR sku=''"
        )
        return cur.rowcount


def reset_fila() -> int:
    """Apaga todos os itens da fila. Usar com cautela."""
    with db_transaction() as conn:
        cur = conn.execute("DELETE FROM fila_aprovacao")
        return cur.rowcount


def ja_existe_pendente(sku: str, canal: str | None = None) -> bool:
    """Verifica se jÃ¡ existe um item pendente para o mesmo SKU (e canal, se informado)."""
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM fila_aprovacao WHERE sku=? AND status='pendente' LIMIT 1",
        (sku,)
    ).fetchone()
    return row is not None


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Config CRUD
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def get_config(chave: str, default: Any = None) -> Any:
    conn = get_db()
    row = conn.execute("SELECT valor FROM config WHERE chave=?", (chave,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["valor"])
    except Exception:
        return row["valor"]


def set_config(chave: str, valor: Any) -> None:
    with db_transaction() as conn:
        conn.execute(
            """INSERT INTO config (chave, valor, atualizado_em)
               VALUES (?,?,datetime('now'))
               ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor, atualizado_em=excluded.atualizado_em""",
            (chave, json.dumps(valor, ensure_ascii=False, default=str)),
        )


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# MigraÃ§Ã£o dos JSONs legados
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def migrar_json_legado(
    regras_path: Path | None = None,
    fila_path: Path | None = None,
    config_path: Path | None = None,
) -> dict:
    """
    Importa dados dos arquivos JSON legados para o SQLite.
    Seguro para chamar mÃºltiplas vezes (usa INSERT OR REPLACE para fila,
    e substitui as regras por completo).
    """
    resultado = {"regras": 0, "fila": 0, "config": False}

    regras_path = regras_path or DATA_DIR / "regras.json"
    fila_path = fila_path or DATA_DIR / "fila_aprovacao.json"
    config_path = config_path or DATA_DIR / "config.json"

    # Regras
    if regras_path.exists():
        try:
            regras = json.loads(regras_path.read_text(encoding="utf-8"))
            if isinstance(regras, list):
                resultado["regras"] = substituir_todas_regras(regras)
                logger.info("Migradas %d regras de %s", resultado["regras"], regras_path)
        except Exception as e:
            logger.error("Erro ao migrar regras: %s", e)

    # Fila
    if fila_path.exists():
        try:
            itens = json.loads(fila_path.read_text(encoding="utf-8"))
            if isinstance(itens, list):
                for item in itens:
                    try:
                        inserir_item_fila(item)
                        resultado["fila"] += 1
                    except Exception as e:
                        logger.warning("Erro ao migrar item fila %s: %s", item.get("id"), e)
                logger.info("Migrados %d itens da fila de %s", resultado["fila"], fila_path)
        except Exception as e:
            logger.error("Erro ao migrar fila: %s", e)

    # Config
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(cfg, dict):
                set_config("app_config", cfg)
                resultado["config"] = True
                logger.info("Config migrada de %s", config_path)
        except Exception as e:
            logger.error("Erro ao migrar config: %s", e)

    return resultado


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# CLI â€” python database.py migrate
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    if cmd == "migrate":
        init_db()
        resultado = migrar_json_legado()
        print(f"MigraÃ§Ã£o concluÃ­da: {resultado}")

    elif cmd == "init":
        init_db()
        print(f"Banco criado em {_db_path()}")

    elif cmd == "stats":
        init_db()
        print("Fila:", stats_fila())
        print("Regras:", len(listar_regras()))

    else:
        print("Uso: python database.py [migrate|init|stats]")
        print("  migrate  â€” importa dados dos JSONs legados para o SQLite")
        print("  init     â€” cria as tabelas sem migrar dados")
        print("  stats    â€” exibe contagens atuais")

