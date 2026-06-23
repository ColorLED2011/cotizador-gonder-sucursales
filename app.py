"""
Cotizador Sucursales GONDER
Backend Flask â Odoo v19 XML-RPC + Scraper BCV
ProducciÃ³n: Render.com
"""

import os
import re
import time
import logging
import xmlrpc.client
from threading import Lock, Thread

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request
from dotenv import load_dotenv

# âââ ConfiguraciÃ³n inicial ââââââââââââââââââââââââââââââââââââââââââââââââââââ
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# âââ Variables de entorno âââââââââââââââââââââââââââââââââââââââââââââââââââââ
ODOO_URL      = os.environ.get("ODOO_URL", "")
ODOO_DB       = os.environ.get("ODOO_DB", "")
ODOO_USER     = os.environ.get("ODOO_USER", "")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD", "")


# âââ Cache de tasa BCV âââââââââââââââââââââââââââââââââââââââââââââââââââââââ
_tasa_cache: dict = {"valor": None, "timestamp": 0}
_tasa_lock = Lock()
CACHE_TTL = 1800  # 30 minutos

# Cache de pricelist items (5 min TTL)
_pl_items_cache = {}
_pl_items_lock = Lock()
PL_CACHE_TTL = 300


def _get_pl_items(pl_id):
    """Devuelve (items, categ_parents) con cache de 5 minutos."""
    with _pl_items_lock:
        entry = _pl_items_cache.get(pl_id)
        if entry and (time.time() - entry["ts"]) < PL_CACHE_TTL:
            return entry["items"], entry["categ_parents"]
    try:
        items = odoo_call(
            "product.pricelist.item", "search_read",
            [[["pricelist_id", "=", pl_id]]],
            {"fields": ["product_id", "product_tmpl_id", "categ_id",
                        "fixed_price", "compute_price",
                        "percent_price", "applied_on"],
             "limit": 1000},
        )
    except Exception:
        items = []
    categ_parents = {}
    if items:
        cat_ids = list({i["categ_id"][0] for i in items
                        if i.get("applied_on") == "2_product_category"
                        and isinstance(i.get("categ_id"), list)})
        to_fetch = set(cat_ids)
        for _ in range(6):
            if not to_fetch:
                break
            try:
                cats = odoo_call("product.category", "search_read",
                    [[["id", "in", list(to_fetch)]]],
                    {"fields": ["id", "parent_id"], "limit": 500})
            except Exception:
                break
            nxt = set()
            for cat in cats:
                pid = cat["parent_id"][0] if isinstance(cat.get("parent_id"), list) else None
                categ_parents[cat["id"]] = pid
                if pid and pid not in categ_parents:
                    nxt.add(pid)
            to_fetch = nxt
    with _pl_items_lock:
        _pl_items_cache[pl_id] = {"items": items, "categ_parents": categ_parents, "ts": time.time()}
    return items, categ_parents



# âââ Scraper BCV âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
def _scrape_bcv() -> float | None:
    """Extrae la tasa USD del Banco Central de Venezuela."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "es-VE,es;q=0.9",
    }

    try:
        resp = requests.get("https://www.bcv.org.ve/", headers=headers, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        dolar_div = soup.select_one("#dolar strong")
        if dolar_div:
            raw = dolar_div.get_text(strip=True).replace(",", ".")
            return float(raw)

        matches = re.findall(r"\b(\d{1,3}[,.]\d{2,4})\b", resp.text)
        for m in matches:
            val = float(m.replace(",", "."))
            if 10 < val < 999999:
                log.warning("BCV: tasa extraÃ­da por regex de respaldo: %s", val)
                return val

    except Exception as exc:
        log.error("BCV principal fallÃ³: %s", exc)

    # Fallback: ExchangeRate-API
    try:
        r2 = requests.get("https://open.er-api.com/v6/latest/USD", timeout=8)
        data = r2.json()
        ves = data.get("rates", {}).get("VES")
        if ves:
            log.info("Tasa obtenida de ExchangeRate-API (fallback): %s", ves)
            return float(ves)
    except Exception as exc:
        log.error("ExchangeRate-API fallback fallÃ³: %s", exc)

    return None


def get_tasa_bcv() -> float:
    """Devuelve la tasa BCV cacheada. Refresca cada CACHE_TTL segundos."""
    with _tasa_lock:
        now = time.time()
        if _tasa_cache["valor"] is None or (now - _tasa_cache["timestamp"]) > CACHE_TTL:
            log.info("Actualizando tasa BCVâ¦")
            nueva = _scrape_bcv()
            if nueva:
                _tasa_cache["valor"] = nueva
                _tasa_cache["timestamp"] = now
                log.info("Tasa BCV actualizada: %s", nueva)
            else:
                log.warning("No se pudo obtener tasa BCV; se mantiene la Ãºltima conocida.")
        return _tasa_cache["valor"] or 0.0


# âââ ConexiÃ³n Odoo XML-RPC âââââââââââââââââââââââââââââââââââââââââââââââââââ
def _odoo_common():
    return xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")


def _odoo_models():
    return xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")


def odoo_uid() -> int:
    """Autentica y devuelve el UID del usuario de servicio."""
    try:
        common = _odoo_common()
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if not uid:
            raise ValueError("Credenciales Odoo invÃ¡lidas.")
        return uid
    except Exception as exc:
        log.error("Error autenticando en Odoo: %s", exc)
        raise


def odoo_call(model: str, method: str, args: list, kwargs: dict | None = None) -> any:
    """Wrapper centralizado para llamadas execute_kw a Odoo."""
    uid = odoo_uid()
    models = _odoo_models()
    return models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        model, method, args, kwargs or {},
    )


# âââ LÃ³gica de Precios âââââââââââââââââââââââââââââââââââââââââââââââââââââââ
def _precio_en_lista(product_id: int, pricelist_id: int, qty: float = 1.0) -> float:
    """
    Obtiene el precio de un producto en una lista de precio especÃ­fica.
    Consulta product.pricelist.item para mayor control.
    """
    items = odoo_call(
        "product.pricelist.item",
        "search_read",
        [[
            ["pricelist_id", "=", pricelist_id],
            "|",
            ["product_id", "=", product_id],
            ["product_tmpl_id.product_variant_ids", "in", [product_id]],
        ]],
        {
            "fields": ["fixed_price", "compute_price", "percent_price",
                       "price_discount", "applied_on"],
            "limit": 1,
            "order": "applied_on asc",
        },
    )
    if items:
        item = items[0]
        if item["compute_price"] == "fixed":
            return item["fixed_price"]
        return 0.0

    prod = odoo_call(
        "product.product",
        "search_read",
        [[["id", "=", product_id]]],
        {"fields": ["lst_price"], "limit": 1},
    )
    return prod[0]["lst_price"] if prod else 0.0


# âââ Helper de producto âââââââââââââââââââââââââââââââââââââââââââââââââââââââ
def _build_producto_dict(p: dict, pl_estandar: int | None, pl_bcv: int | None, tasa: float) -> dict:
    """Construye el dict estÃ¡ndar de un producto con precios calculados."""
    precio_estandar = (
        _precio_en_lista(p["id"], pl_estandar) if pl_estandar else p["lst_price"]
    ) or p["lst_price"]

    precio_bcv = (
        _precio_en_lista(p["id"], pl_bcv) if pl_bcv else p["lst_price"]
    ) or p["lst_price"]

    return {
        "id":              p["id"],
        "nombre":          p["name"],
        "codigo":          p.get("default_code") or "",
        "barcode":         p.get("barcode") or "",
        "uom":             p["uom_id"][1] if p.get("uom_id") else "",
        "imagen":          p.get("image_128") or "",
        "precio_estandar": round(precio_estandar, 2),
        "precio_bcv":      round(precio_bcv, 2),
        "precio_bcv_bs":   round(precio_bcv * tasa, 2),
        "tasa_bcv":        tasa,
    }


# âââ API ENDPOINTS ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

@app.route("/api/ping")
def api_ping():
    return jsonify({"ok": True})


@app.route("/api/debug-pricelist/<int:pl_id>")
def api_debug_pricelist(pl_id):
    """Devuelve items y categ_ids de la lista de precio. q=cÃ³digo para ver categ de producto."""
    q = request.args.get("q", "")
    try:
        items = odoo_call(
            "product.pricelist.item", "search_read",
            [[["pricelist_id", "=", pl_id]]],
            {"fields": ["product_id", "product_tmpl_id", "categ_id", "compute_price",
                        "fixed_price", "percent_price", "applied_on"],
             "limit": 100},
        )
        cat_items = [i for i in items if i.get("applied_on") == "2_product_category"]
        categ_ids_in_pl = {(i["categ_id"][0] if isinstance(i["categ_id"], list) else i["categ_id"]): i["percent_price"] for i in cat_items if i.get("categ_id")}

        prod_info = None
        if q:
            prods = odoo_call("product.product", "search_read",
                [[["default_code", "=", q.upper()], ["active", "=", True]]],
                {"fields": ["id", "name", "categ_id", "lst_price"], "limit": 1})
            if prods:
                p = prods[0]
                cid = p["categ_id"][0] if isinstance(p.get("categ_id"), list) else p.get("categ_id")
                prod_info = {"id": p["id"], "name": p["name"], "categ_id": cid,
                             "categ_name": p["categ_id"][1] if isinstance(p.get("categ_id"), list) else None,
                             "lst_price": p["lst_price"],
                             "categ_in_pricelist": cid in categ_ids_in_pl,
                             "pct_if_matched": categ_ids_in_pl.get(cid)}
        return jsonify({"pl_id": pl_id, "total_items": len(items),
                        "category_rules": categ_ids_in_pl, "product": prod_info})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/")

def index():
    return render_template("index.html")


@app.route("/api/listas-precio")

def api_listas_precio():
    """Devuelve todas las listas de precio activas del Odoo del cliente."""
    try:
        listas = odoo_call(
            "product.pricelist",
            "search_read",
            [[["active", "=", True]]],
            {"fields": ["id", "name", "currency_id"], "order": "name asc"},
        )
        return jsonify({"listas": listas})
    except Exception as exc:
        log.error("Error leyendo listas de precio: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/tasa")

def api_tasa():
    """Devuelve la tasa BCV del dÃ­a."""
    tasa = get_tasa_bcv()
    return jsonify({
        "tasa": tasa,
        "moneda": "VES",
        "fuente": "BCV",
        "ok": tasa > 0,
    })


@app.route("/api/productos")

def api_productos():
    """
    Busca productos por nombre, cÃ³digo de barras, o devuelve catÃ¡logo completo.
    Query params:
      q           â texto libre (nombre / cÃ³digo interno)
      barcode     â cÃ³digo de barras exacto
      catalogo    â "1" para listar todos (sin q ni barcode)
      limit       â mÃ¡ximo de resultados (default 30; catÃ¡logo default 60)
      pl_estandar â ID de lista de precio EstÃ¡ndar (elegida en la UI)
      pl_bcv      â ID de lista de precio BCV      (elegida en la UI)
    """
    q           = request.args.get("q", "").strip()
    barcode     = request.args.get("barcode", "").strip()
    catalogo    = request.args.get("catalogo", "0") == "1"
    pl_estandar = request.args.get("pl_estandar", type=int)
    pl_bcv      = request.args.get("pl_bcv",      type=int)
    limit       = int(request.args.get("limit", 100 if catalogo else 30))
    offset      = int(request.args.get("offset", 0))

    if not q and not barcode and not catalogo:
        return jsonify({"error": "Se requiere 'q', 'barcode', o catalogo=1"}), 400

    try:
        domain: list = [["active", "=", True], ["sale_ok", "=", True]]

        if barcode:
            domain.append(["barcode", "=", barcode])
        elif q:
            domain.extend(["|", ["name", "ilike", q], ["default_code", "ilike", q]])
        # catalogo=1 â sin filtro adicional, devuelve todos los productos activos

        _fields = ["id", "name", "default_code", "barcode",
                   "lst_price", "uom_id", "product_tmpl_id", "categ_id"]
        if not catalogo:
            _fields.append("image_128")
        productos_raw = odoo_call(
            "product.product",
            "search_read",
            [domain],
            {
                "fields": _fields,
                "limit": limit,
                "offset": offset,
                "order": "name asc",
            },
        )

        tasa = get_tasa_bcv()

        # ââ Batch price lookup: fijo (variante/plantilla) + % por categorÃ­a ââ
        def _batch_precios(pl_id, prod_ids, tmpl_ids, categ_ids, lst_prices):
            if not pl_id or not prod_ids:
                return {}
            items, categ_parents = _get_pl_items(pl_id)
            if not items:
                return {}
            result  = {}
            cat_pct = {}
            prod_set = set(prod_ids)
            for item in items:
                cp  = item.get("compute_price", "")
                aon = item.get("applied_on", "")
                if aon == "0_product_variant" and item.get("product_id") and cp == "fixed":
                    pid = item["product_id"][0]
                    if pid in prod_set and pid not in result:
                        result[pid] = item["fixed_price"]
                elif aon == "1_product" and item.get("product_tmpl_id") and cp == "fixed":
                    tid = item["product_tmpl_id"][0]
                    for p_id, t_id in zip(prod_ids, tmpl_ids):
                        if t_id == tid and p_id not in result:
                            result[p_id] = item["fixed_price"]
                elif aon == "2_product_category" and item.get("categ_id") and cp == "percentage":
                    cid = item["categ_id"][0]
                    if cid not in cat_pct:
                        cat_pct[cid] = item["percent_price"]
            def _resolve_categ(cid):
                visited, cur = set(), cid
                while cur and cur not in visited:
                    if cur in cat_pct:
                        return cur
                    visited.add(cur)
                    cur = categ_parents.get(cur)
                return None
            for p_id, c_id, base in zip(prod_ids, categ_ids, lst_prices):
                if p_id not in result:
                    eff = _resolve_categ(c_id)
                    if eff is not None:
                        pct = cat_pct[eff]
                        result[p_id] = round(base * (1 - pct / 100), 4)
            return result

        prod_ids   = [p["id"] for p in productos_raw]
        tmpl_ids   = [(p["product_tmpl_id"][0] if isinstance(p.get("product_tmpl_id"), list)
                       else p.get("product_tmpl_id") or 0) for p in productos_raw]
        categ_ids  = [(p["categ_id"][0] if isinstance(p.get("categ_id"), list)
                       else p.get("categ_id") or 0) for p in productos_raw]
        lst_prices = [p["lst_price"] for p in productos_raw]

        pe_map = _batch_precios(pl_estandar, prod_ids, tmpl_ids, categ_ids, lst_prices)
        pb_map = _batch_precios(pl_bcv,      prod_ids, tmpl_ids, categ_ids, lst_prices)

        resultado = []
        for p in productos_raw:
            pe = pe_map.get(p["id"]) or p["lst_price"]
            pb = pb_map.get(p["id"]) or p["lst_price"]
            resultado.append({
                "id":              p["id"],
                "nombre":          p["name"],
                "codigo":          p.get("default_code") or "",
                "barcode":         p.get("barcode") or "",
                "uom":             p["uom_id"][1] if p.get("uom_id") else "",
                "imagen":          p.get("image_128") or "",
                "precio_estandar": round(pe, 2),
                "precio_bcv":      round(pb, 2),
                "precio_bcv_bs":   round(pb * tasa, 2),
                "tasa_bcv":        tasa,
            })

        return jsonify({"productos": resultado, "total": len(resultado)})

    except Exception as exc:
        log.error("Error buscando productos: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/producto/<int:product_id>")
def api_producto_detalle(product_id):
    pl_estandar = request.args.get("pl_estandar", type=int)
    pl_bcv      = request.args.get("pl_bcv",      type=int)
    try:
        prods = odoo_call(
            "product.product", "search_read",
            [[["id", "=", product_id]]],
            {"fields": ["id","name","default_code","barcode",
                        "lst_price","uom_id","image_256","description_sale"], "limit": 1},
        )
        if not prods:
            return jsonify({"error": "Producto no encontrado"}), 404
        p    = prods[0]
        tasa = get_tasa_bcv()
        precio_estandar = (_precio_en_lista(p["id"], pl_estandar) if pl_estandar else p["lst_price"]) or p["lst_price"]
        precio_bcv      = (_precio_en_lista(p["id"], pl_bcv)      if pl_bcv      else p["lst_price"]) or p["lst_price"]
        try:
            pkgs = odoo_call("product.packaging","search_read",
                [[["product_id","=",product_id]]],
                {"fields":["name","qty"],"limit":5,"order":"qty asc"})
            embalajes = [{"nombre":pk["name"],"qty":pk["qty"]} for pk in pkgs]
        except Exception:
            embalajes = []
        return jsonify({
            "id": p["id"], "nombre": p["name"],
            "codigo": p.get("default_code") or "", "barcode": p.get("barcode") or "",
            "descripcion": p.get("description_sale") or "",
            "uom": p["uom_id"][1] if p.get("uom_id") else "",
            "imagen": p.get("image_256") or "",
            "precio_estandar": round(precio_estandar, 2),
            "precio_bcv":      round(precio_bcv, 2),
            "precio_bcv_bs":   round(precio_bcv * tasa, 2),
            "tasa_bcv": tasa, "embalajes": embalajes,
        })
    except Exception as exc:
        log.error("Error obteniendo detalle del producto %s: %s", product_id, exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/orden", methods=["POST"])
def api_crear_orden():
    data       = request.get_json(force=True)
    vendedor   = data.get("vendedor", "").strip()
    cliente_id = data.get("cliente_id")
    pl_bcv     = data.get("pl_bcv")
    items      = data.get("items", [])
    notas      = data.get("notas", "")
    if not cliente_id or not items:
        return jsonify({"error": "cliente_id e items son obligatorios"}), 400
    try:
        order_vals = {"partner_id": cliente_id, "client_order_ref": vendedor,
                      "state": "draft", "note": notas}
        if pl_bcv:
            order_vals["pricelist_id"] = int(pl_bcv)
        order_id = odoo_call("sale.order", "create", [order_vals])
        for item in items:
            odoo_call("sale.order.line", "create", [{
                "order_id": order_id, "product_id": item["product_id"],
                "product_uom_qty": float(item.get("qty", 1)),
                "price_unit": float(item.get("precio_bcv", 0)),
            }])
        return jsonify({"ok": True, "order_id": order_id,
                        "mensaje": f"Pedido #{order_id} creado en Odoo (Borrador, Tarifa BCV)."})
    except Exception as exc:
        log.error("Error creando orden: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/clientes")
def api_clientes():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"clientes": []})
    try:
        clientes = odoo_call(
            "res.partner", "search_read",
            [[["name","ilike",q],["customer_rank",">",0]]],
            {"fields":["id","name","vat","phone"],"limit":15},
        )
        return jsonify({"clientes": clientes})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# âââ Keep-alive para Render Free âââââââââââââââââââââââââââââââââââââââââââââ
def _keep_alive():
    time.sleep(60)
    app_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    if not app_url:
        return
    ping_url = f"{app_url}/api/ping"
    while True:
        try:
            requests.get(ping_url, timeout=10)
        except Exception:
            pass
        time.sleep(14 * 60)


# âââ Punto de entrada âââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
if __name__ == "__main__":
    if os.environ.get("RENDER_EXTERNAL_URL"):
        Thread(target=_keep_alive, daemon=True).start()
    port  = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV", "production") == "development"
    app.run(host="0.0.0.0", port=port, debug=debug)
