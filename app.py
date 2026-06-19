import os
import xmlrpc.client
import requests
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

# ── Odoo config ──────────────────────────────────────────────────────────────
ODOO_URL      = os.environ.get("ODOO_URL", "https://gonder.odoo.com")
ODOO_DB       = os.environ.get("ODOO_DB", "gonder")
ODOO_USER     = os.environ.get("ODOO_USER", "")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD", "")

# ── Telegram config ──────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── Pricelists disponibles ───────────────────────────────────────────────────
PRICELISTS = ["USD BCV", "USD"]


def odoo_connect():
    """Devuelve (uid, models_proxy) o lanza excepción."""
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    try:
        version = common.version()
        server_ver = version.get("server_version", "?")
    except Exception as e:
        raise ConnectionError(f"No se puede conectar a Odoo ({ODOO_URL}): {e}")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        raise ConnectionError(
            f"Autenticación fallida (Odoo {server_ver}). "
            f"DB='{ODOO_DB}' USER='{ODOO_USER}' — "
            f"verifica contraseña o genera una API Key en Odoo."
        )
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return uid, models


def get_pricelist_id(models, uid, pricelist_name):
    """Busca el ID de una tarifa por nombre."""
    result = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.pricelist", "search_read",
        [[["name", "=", pricelist_name]]],
        {"fields": ["id", "name"], "limit": 1}
    )
    return result[0]["id"] if result else None


def get_price_from_pricelist(models, uid, pricelist_id, product_id, qty=1):
    """Obtiene el precio de un producto para una tarifa y cantidad dada.
    En Odoo 17+ price_get fue eliminado; se usa el campo 'price' con contexto.
    """
    try:
        result = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "product.product", "read",
            [[product_id]],
            {
                "fields": ["price"],
                "context": {"pricelist": pricelist_id, "quantity": qty}
            }
        )
        if result:
            return result[0].get("price", 0)
        return 0
    except Exception:
        return 0


def get_packaging(models, uid, product_tmpl_id):
    """Obtiene packaging del producto (ej: caja con X m²).
    Retorna lista vacía si el modelo no existe en esta instalación de Odoo.
    """
    try:
        packaging = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "product.packaging", "search_read",
            [[["product_tmpl_id", "=", product_tmpl_id]]],
            {"fields": ["id", "name", "qty", "product_uom_id"], "limit": 5}
        )
        return packaging
    except Exception:
        return []


# ── Endpoints ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/catalog")
def catalog():
    """Devuelve todos los productos activos con precios en ambas tarifas.
    Usa llamadas en batch para evitar N+1 queries y timeouts.
    """
    try:
        uid, models = odoo_connect()

        # IDs de ambas tarifas
        pricelist_ids = {}
        for name in PRICELISTS:
            pid = get_pricelist_id(models, uid, name)
            if pid:
                pricelist_ids[name] = pid

        # Todos los productos vendibles activos
        products = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "product.template", "search_read",
            [[["sale_ok", "=", True], ["active", "=", True]]],
            {
                "fields": [
                    "id", "name", "default_code", "image_128",
                    "uom_id", "description_sale", "attribute_line_ids"
                ],
                "limit": 500
            }
        )

        if not products:
            return jsonify({"ok": True, "products": []})

        product_tmpl_ids = [p["id"] for p in products]

        # BATCH: obtener TODAS las variantes de una vez
        all_variants = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "product.product", "search_read",
            [[["product_tmpl_id", "in", product_tmpl_ids], ["active", "=", True]]],
            {"fields": ["id", "default_code", "product_tmpl_id"], "limit": 2000}
        )

        # Mapa: tmpl_id -> primera variante
        variant_map = {}
        all_variant_ids = []
        for v in all_variants:
            raw = v.get("product_tmpl_id")
            tmpl_id = raw[0] if isinstance(raw, list) else raw
            if tmpl_id not in variant_map:
                variant_map[tmpl_id] = v
                all_variant_ids.append(v["id"])

        # BATCH: obtener precios de TODAS las variantes por tarifa de una vez
        price_map = {}  # {pl_name: {variant_id: price}}
        for pl_name, pl_id in pricelist_ids.items():
            try:
                if all_variant_ids:
                    results = models.execute_kw(
                        ODOO_DB, uid, ODOO_PASSWORD,
                        "product.product", "read",
                        [all_variant_ids],
                        {"fields": ["price"], "context": {"pricelist": pl_id, "quantity": 1}}
                    )
                    price_map[pl_name] = {r["id"]: r.get("price", 0) for r in results}
                else:
                    price_map[pl_name] = {}
            except Exception:
                price_map[pl_name] = {}

        # BATCH: obtener TODOS los packagings de una vez
        packaging_map = {}  # {tmpl_id: [list of packaging]}
        try:
            all_packaging = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "product.packaging", "search_read",
                [[["product_tmpl_id", "in", product_tmpl_ids]]],
                {"fields": ["id", "name", "qty", "product_uom_id", "product_tmpl_id"], "limit": 2000}
            )
            for pk in all_packaging:
                raw_tmpl = pk.get("product_tmpl_id")
                tmpl_id = raw_tmpl[0] if isinstance(raw_tmpl, list) else raw_tmpl
                if tmpl_id not in packaging_map:
                    packaging_map[tmpl_id] = []
                packaging_map[tmpl_id].append(pk)
        except Exception:
            pass  # packaging no disponible en este Odoo

        # Ensamblar catálogo en Python (sin más llamadas a Odoo)
        catalog_items = []
        for p in products:
            tmpl_id = p["id"]
            variant = variant_map.get(tmpl_id)
            if not variant:
                continue
            variant_id = variant["id"]

            prices = {
                pl_name: price_map.get(pl_name, {}).get(variant_id, 0)
                for pl_name in PRICELISTS
            }

            uom_name = p["uom_id"][1] if p.get("uom_id") else ""
            packaging = packaging_map.get(tmpl_id, [])
            m2_per_box = None
            box_packaging = None
            for pk in packaging:
                pk_name_lower = (pk.get("name") or "").lower()
                if any(x in pk_name_lower for x in ["caja", "box", "paq", "pack"]):
                    box_packaging = pk
                    m2_per_box = pk.get("qty")
                    break

            item = {
                "id": tmpl_id,
                "variant_id": variant_id,
                "name": p["name"],
                "code": p.get("default_code") or variant.get("default_code") or "",
                "image": p.get("image_128") or "",
                "uom": uom_name,
                "prices": prices,
                "m2_per_box": m2_per_box,
                "box_packaging_name": box_packaging["name"] if box_packaging else None,
            }
            catalog_items.append(item)

        return jsonify({"ok": True, "products": catalog_items})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/product/<int:product_tmpl_id>")
def product_detail(product_tmpl_id):
    """Ficha técnica: imagen grande + atributos + especificaciones."""
    try:
        uid, models = odoo_connect()

        product = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "product.template", "read",
            [product_tmpl_id],
            {
                "fields": [
                    "id", "name", "default_code", "image_1920",
                    "description_sale", "uom_id",
                    "attribute_line_ids", "product_variant_ids"
                ]
            }
        )
        if not product:
            return jsonify({"ok": False, "error": "Producto no encontrado"}), 404
        p = product[0]

        # Atributos
        attrs = []
        if p.get("attribute_line_ids"):
            attr_lines = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "product.template.attribute.line", "read",
                [p["attribute_line_ids"]],
                {"fields": ["attribute_id", "value_ids"]}
            )
            for line in attr_lines:
                attr_name = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD,
                    "product.attribute", "read",
                    [line["attribute_id"][0]],
                    {"fields": ["name"]}
                )[0]["name"]
                values = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD,
                    "product.attribute.value", "read",
                    [line["value_ids"]],
                    {"fields": ["name"]}
                )
                attrs.append({
                    "attr": attr_name,
                    "values": [v["name"] for v in values]
                })

        # Packaging
        packaging = get_packaging(models, uid, product_tmpl_id)

        return jsonify({
            "ok": True,
            "product": {
                "id": p["id"],
                "name": p["name"],
                "code": p.get("default_code") or "",
                "image": p.get("image_1920") or "",
                "uom": p["uom_id"][1] if p.get("uom_id") else "",
                "description": p.get("description_sale") or "",
                "attributes": attrs,
                "packaging": packaging,
            }
        })

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/search_product")
def search_product():
    """Busca un producto por código exacto y devuelve info básica + precios."""
    code = request.args.get("code", "").strip()
    pricelist_name = request.args.get("pricelist", PRICELISTS[0])
    if not code:
        return jsonify({"ok": False, "error": "Código vacío"}), 400

    try:
        uid, models = odoo_connect()

        # Buscar por código en variante o template
        variants = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "product.product", "search_read",
            [[["default_code", "=ilike", code], ["active", "=", True]]],
            {"fields": ["id", "name", "default_code", "product_tmpl_id", "image_128", "uom_id"], "limit": 1}
        )
        if not variants:
            # Buscar en template
            templates = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "product.template", "search_read",
                [[["default_code", "=ilike", code], ["active", "=", True]]],
                {"fields": ["id", "name", "default_code", "uom_id"], "limit": 1}
            )
            if not templates:
                return jsonify({"ok": False, "error": f"Producto '{code}' no encontrado"}), 404
            t = templates[0]
            variant_list = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "product.product", "search_read",
                [[["product_tmpl_id", "=", t["id"]], ["active", "=", True]]],
                {"fields": ["id", "image_128"], "limit": 1}
            )
            variant_id = variant_list[0]["id"] if variant_list else None
            image = variant_list[0].get("image_128") if variant_list else None
            tmpl_id = t["id"]
            name = t["name"]
            uom = t["uom_id"][1] if t.get("uom_id") else ""
        else:
            v = variants[0]
            variant_id = v["id"]
            tmpl_id = v["product_tmpl_id"][0]
            name = v["name"]
            image = v.get("image_128")
            uom = v["uom_id"][1] if v.get("uom_id") else ""

        # Precio en la tarifa seleccionada
        pl_id = get_pricelist_id(models, uid, pricelist_name)
        price = get_price_from_pricelist(models, uid, pl_id, variant_id) if pl_id and variant_id else 0

        # Precio por m² si aplica (packaging)
        packaging = get_packaging(models, uid, tmpl_id)
        m2_per_box = None
        box_name = None
        for pk in packaging:
            pk_name_lower = (pk.get("name") or "").lower()
            if any(x in pk_name_lower for x in ["caja", "box", "paq", "pack"]):
                m2_per_box = pk.get("qty")
                box_name = pk.get("name")
                break

        price_per_m2 = None
        price_per_box = None
        if m2_per_box and m2_per_box > 0 and price > 0:
            if uom.lower() in ["m²", "m2", "metro cuadrado"]:
                # precio ya es por m², calcular por caja
                price_per_m2 = price
                price_per_box = price * m2_per_box
            else:
                # precio es por caja, calcular por m²
                price_per_box = price
                price_per_m2 = price / m2_per_box

        return jsonify({
            "ok": True,
            "product": {
                "variant_id": variant_id,
                "tmpl_id": tmpl_id,
                "name": name,
                "code": code,
                "image": image or "",
                "uom": uom,
                "price": price,
                "price_per_m2": price_per_m2,
                "price_per_box": price_per_box,
                "m2_per_box": m2_per_box,
                "box_name": box_name,
                "pricelist": pricelist_name,
            }
        })

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/create_order", methods=["POST"])
def create_order():
    """Crea un sale.order borrador en Odoo y notifica por Telegram."""
    data = request.get_json()
    vendedor    = data.get("vendedor", "")
    cliente     = data.get("cliente", "")
    pricelist   = data.get("pricelist", PRICELISTS[0])
    lines       = data.get("lines", [])  # [{variant_id, qty, price, name, code}]
    nota        = data.get("nota", "")

    if not lines:
        return jsonify({"ok": False, "error": "Sin líneas de pedido"}), 400

    try:
        uid, models = odoo_connect()

        # Tarifa
        pl_id = get_pricelist_id(models, uid, pricelist)

        # Buscar o crear contacto genérico "Sucursal" si no existe cliente Odoo
        partner_ids = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "res.partner", "search",
            [[["name", "ilike", cliente]]],
            {"limit": 1}
        )
        if partner_ids:
            partner_id = partner_ids[0]
        else:
            # Crear cliente nuevo
            partner_id = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "res.partner", "create",
                [{"name": cliente, "customer_rank": 1}]
            )

        # Armar líneas del pedido
        order_lines = []
        for line in lines:
            order_lines.append((0, 0, {
                "product_id": line["variant_id"],
                "product_uom_qty": line["qty"],
                "price_unit": line["price"],
            }))

        # Nota interna: vendedor + nota del usuario
        note_parts = [f"Vendedor: {vendedor}"]
        if nota:
     