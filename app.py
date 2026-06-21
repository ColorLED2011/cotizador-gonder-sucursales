"""
Cotizador Sucursales GONDER
Backend Flask — Odoo v19 XML-RPC + Scraper BCV
Producción: Render.com
"""

import os
import re
import time
import logging
import xmlrpc.client
from functools import wraps
from threading import Lock, Thread

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request, session, redirect, url_for
from dotenv import load_dotenv

# ─── Configuración inicial ────────────────────────────────────────────────────
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# ─── Variables de entorno ─────────────────────────────────────────────────────
ODOO_URL      = os.environ.get("ODOO_URL", "")
ODOO_DB       = os.environ.get("ODOO_DB", "")
ODOO_USER     = os.environ.get("ODOO_USER", "")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD", "")
SECRET_KEY    = os.environ.get("SECRET_KEY", "gonder-secret-2025")
APP_PASSWORD  = os.environ.get("APP_PASSWORD", "")

app.secret_key = SECRET_KEY

# ─── Login / Acceso ──────────────────────────────────────────────────────────
def login_required(f):
    """Decorador: redirige al login si el vendedor no está autenticado."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if APP_PASSWORD and not session.get("autenticado"):
            # Las llamadas API devuelven 401 en vez de redirigir
            if request.path.startswith("/api/"):
                return jsonify({"error": "No autorizado"}), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        clave = request.form.get("clave", "").strip()
        if clave == APP_PASSWORD:
            session["autenticado"] = True
            session.permanent = True
            return redirect(url_for("index"))
        error = "Clave incorrecta. Intenta de nuevo."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ─── Cache de tasa BCV ───────────────────────────────────────────────────────
_tasa_cache: dict = {"valor": None, "timestamp": 0}
_tasa_lock = Lock()
CACHE_TTL = 1800  # 30 minutos


# ─── Scraper BCV ─────────────────────────────────────────────────────────────
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
                log.warning("BCV: tasa extraída por regex de respaldo: %s", val)
                return val

    except Exception as exc:
        log.error("BCV principal falló: %s", exc)

    # Fallback: ExchangeRate-API
    try:
        r2 = requests.get("https://open.er-api.com/v6/latest/USD", timeout=8)
        data = r2.json()
        ves = data.get("rates", {}).get("VES")
        if ves:
            log.info("Tasa obtenida de ExchangeRate-API (fallback): %s", ves)
            return float(ves)
    except Exception as exc:
        log.error("ExchangeRate-API fallback falló: %s", exc)

    return None


def get_tasa_bcv() -> float:
    """Devuelve la tasa BCV cacheada. Refresca cada CACHE_TTL segundos."""
    with _tasa_lock:
        now = time.time()
        if _tasa_cache["valor"] is None or (now - _tasa_cache["timestamp"]) > CACHE_TTL:
            log.info("Actualizando tasa BCV…")
            nueva = _scrape_bcv()
            if nueva:
                _tasa_cache["valor"] = nueva
                _tasa_cache["timestamp"] = now
                log.info("Tasa BCV actualizada: %s", nueva)
            else:
                log.warning("No se pudo obtener tasa BCV; se mantiene la última conocida.")
        return _tasa_cache["valor"] or 0.0


# ─── Conexión Odoo XML-RPC ───────────────────────────────────────────────────
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
            raise ValueError("Credenciales Odoo inválidas.")
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


# ─── Lógica de Precios ───────────────────────────────────────────────────────
def _precio_en_lista(product_id: int, pricelist_id: int, qty: float = 1.0) -> float:
    """
    Obtiene el precio de un producto en una lista de precio específica.
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


# ─── Helper de producto ───────────────────────────────────────────────────────
def _build_producto_dict(p: dict, pl_estandar: int | None, pl_bcv: int | None, tasa: float) -> dict:
    """Construye el dict estándar de un producto con precios calculados."""
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


# ─── API ENDPOINTS ────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/api/listas-precio")
@login_required
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
@login_required
def api_tasa():
    """Devuelve la tasa BCV del día."""
    tasa = get_tasa_bcv()
    return jsonify({
        "tasa": tasa,
        "moneda": "VES",
        "fuente": "BCV",
        "ok": tasa > 0,
    })


@app.route("/api/productos")
@login_required
def api_productos():
    """
    Busca productos por nombre, código de barras, o devuelve catálogo completo.
    Query params:
      q           — texto libre (nombre / código interno)
      barcode     — código de barras exacto
      catalogo    — "1" para listar todos (sin q ni barcode)
      limit       — máximo de resultados (default 30; catálogo default 60)
      pl_estandar — ID de lista de precio Estándar (elegida en la UI)
      pl_bcv      — ID de lista de precio BCV      (elegida en la UI)
    """
    q           = request.args.get("q", "").strip()
    barcode     = request.args.get("barcode", "").strip()
    catalogo    = request.args.get("catalogo", "0") == "1"
    pl_estandar = request.args.get("pl_estandar", type=int)
    pl_bcv      = request.args.get("pl_bcv",      type=int)
    limit       = int(request.args.get("limit", 60 if catalogo else 30))

    if not q and not barcode and not catalogo:
        return jsonify({"error": "Se requiere 'q', 'barcode', o catalogo=1"}), 400

    try:
        domain: list = [["active", "=", True], ["sale_ok", "=", True]]

        if barcode:
            domain.append(["barcode", "=", barcode])
        elif q:
            domain.extend(["|", ["name", "ilike", q], ["default_code", "ilike", q]])
        # catalogo=1 → sin filtro adicional, devuelve todos los productos activos

        productos_raw = odoo_call(
            "product.product",
            "search_read",
            [domain],
            {
                "fields": [
                    "id", "name", "default_code", "barcode",
                    "lst_price", "uom_id", "image_128",
                ],
                "limit": limit,
                "order": "name asc",
            },
        )

        tasa = get_tasa_bcv()
        resultado = [_build_producto_dict(p, pl_estandar, pl_bcv, tasa) for p in productos_raw]

        return jsonify({"productos": resultado, "total": len(resultado)})

    except Exception as exc:
        log.error("Error buscando productos: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/producto/<int:product_id>")
@login_required
def api_producto_detalle(product_id):
    """
    Detalle completo de un producto para la ficha del catálogo.
    Incluye imagen de mayor resolución y embalajes/packaging.
    """
    pl_estandar = request.args.get("pl_estandar", type=int)
    pl_bcv      = request.args.get("pl_bcv",      type=int)

    try:
        prods = odoo_call(
            "product.product",
            "search_read",
            [[["id", "=", product_id]]],
            {
                "fields": [
                    "id", "name", "default_code", "barcode",
                    "lst_price", "uom_id", "image_256", "description_sale",
                ],
                "limit": 1,
            },
        )
        if not prods:
            return jsonify({"error": "Producto no encontrado"}), 404

        p    = prods[0]
        tasa = get_tasa_bcv()

        precio_estandar = (
            _precio_en_lista(p["id"], pl_estandar) if pl_estandar else p["lst_price"]
        ) or p["lst_price"]

        precio_bcv = (
            _precio_en_lista(p["id"], pl_bcv) if pl_bcv else p["lst_price"]
        ) or p["lst_price"]

        # Embalajes / packaging del producto
        try:
            pkgs = odoo_call(
                "product.packaging",
                "search_read",
                [[["product_id", "=", product_id]]],
                {"fields": ["name", "qty"], "limit": 5, "order": "qty asc"},
            )
            embalajes = [{"nombre": pk["name"], "qty": pk["qty"]} for pk in pkgs]
        except Exception:
            embalajes = []

        return jsonify({
            "id":              p["id"],
            "nombre":          p["name"],
            "codigo":          p.get("default_code") or "",
            "barcode":         p.get("barcode") or "",
            "descripcion":     p.get("description_sale") or "",
            "uom":             p["uom_id"][1] if p.get("uom_id") else "",
            "imagen":          p.get("image_256") or "",
            "precio_estandar": round(precio_estandar, 2),
            "precio_bcv":      round(precio_bcv, 2),
            "precio_bcv_bs":   round(precio_bcv * tasa, 2),
            "tasa_bcv":        tasa,
            "embalajes":       embalajes,
        })

    except Exception as exc:
        log.error("Error obteniendo detalle del producto %s: %s", product_id, exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/orden", methods=["POST"])
@login_required
def api_crear_orden():
    """
    Crea un sale.order en Odoo en estado Borrador usando la Tarifa BCV.
    El nombre del vendedor va en client_order_ref (no requiere usuario Odoo).

    Body JSON:
    {
        "vendedor":   "Nombre Vendedor",
        "cliente_id": 1,
        "pl_bcv":     3,
        "items": [
            {"product_id": 5, "qty": 2, "precio_bcv": 10.50}
        ],
        "notas": "Texto libre"
    }
    """
    data = request.get_json(force=True)

    vendedor   = data.get("vendedor", "").strip()
    cliente_id = data.get("cliente_id")
    pl_bcv     = data.get("pl_bcv")
    items      = data.get("items", [])
    notas      = data.get("notas", "")

    if not cliente_id or not items:
        return jsonify({"error": "cliente_id e items son obligatorios"}), 400

    try:
        order_vals = {
            "partner_id":       cliente_id,
            "client_order_ref": vendedor,
            "state":            "draft",
            "note":             notas,
        }
        if pl_bcv:
            order_vals["pricelist_id"] = int(pl_bcv)

        order_id = odoo_call("sale.order", "create", [order_vals])

        for item in items:
            odoo_call("sale.order.line", "create", [{
                "order_id":        order_id,
                "product_id":      item["product_id"],
                "product_uom_qty": float(item.get("qty", 1)),
                "price_unit":      float(item.get("precio_bcv", 0)),
            }])

        return jsonify({
            "ok":       True,
            "order_id": order_id,
            "mensaje":  f"Pedido #{order_id} creado en Odoo (Borrador, Tarifa BCV).",
        })

    except Exception as exc:
        log.error("Error creando orden: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/clientes")
@login_required
def api_clientes():
    """Busca clientes/partners en Odoo. Param: q"""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"clientes": []})

    try:
        clientes = odoo_call(
            "res.partner",
            "search_read",
            [[["name", "ilike", q], ["customer_rank", ">", 0]]],
            {"fields": ["id", "name", "vat", "phone"], "limit": 15},
        )
        return jsonify({"clientes": clientes})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ─── Salud del servicio ───────────────────────────────────────────────────────
@app.route("/api/ping")
def ping():
    return jsonify({"status": "ok", "app": "Cotizador GONDER"})


# ─── Keep-alive para Render Free ─────────────────────────────────────────────
def _keep_alive():
    time.sleep(60)
    app_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    if not app_url:
        log.info("Keep-alive: RENDER_EXTERNAL_URL no definida — omitido (entorno local).")
        return

    ping_url = f"{app_url}/api/ping"
    log.info("Keep-alive activo → %s (cada 14 min)", ping_url)

    while True:
        try:
            r = requests.get(ping_url, timeout=10)
            log.info("Keep-alive ping OK — status %s", r.status_code)
        except Exception as exc:
            log.warning("Keep-alive ping falló: %s", exc)
        time.sleep(14 * 60)


# ─── Punto de entrada ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    if os.environ.get("RENDER_EXTERNAL_URL"):
        t = Thread(target=_keep_alive, daemon=True)
        t.start()

    port  = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV", "production") == "development"
    app.run(host="0.0.0.0", port=port, debug=debug)
