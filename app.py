import os
import xmlrpc.client
import requests
from flask import Flask, request, jsonify, render_template

app = Flask(__name__)

# ── Credenciales Odoo (desde variables de entorno) ─────────────────────────
ODOO_URL      = os.environ.get("ODOO_URL",      "https://gonder.odoo.com")
ODOO_DB       = os.environ.get("ODOO_DB",       "gonder")
ODOO_USER     = os.environ.get("ODOO_USER",     "")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD", "")

# ── Telegram ────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",   "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── Nombres de tarifas ──────────────────────────────────────────────────────
PRICELIST_NAMES = ["USD BCV", "USD"]


# ── Helpers Odoo ─────────────────────────────────────────────────────────────

def get_odoo():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid    = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        raise ConnectionError("Autenticacion Odoo fallida. Verifica ODOO_USER y ODOO_PASSWORD.")
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return uid, models


def call(models, uid, model, method, args, kwargs=None):
    return models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        model, method, args, kwargs or {}
    )


def get_pricelist_items(models, uid, pl_id):
    """Fetch ALL pricelist.item records for a pricelist in ONE call.
    Returns (by_variant dict, by_template dict, global_item or None).
    """
    items = call(
        models, uid, "product.pricelist.item", "search_read",
        [[["pricelist_id", "=", pl_id]]],
        {"fields": ["applied_on", "product_id", "product_tmpl_id",
                    "compute_price", "fixed_price", "percent_price",
                    "price_discount", "price_surcharge"]}
    )
    by_variant  = {}
    by_template = {}
    global_item = None
    for item in items:
        if item["applied_on"] == "0_product_variant" and item["product_id"]:
            pid = item["product_id"][0] if isinstance(item["product_id"], list) else item["product_id"]
            by_variant[pid] = item
        elif item["applied_on"] == "1_product" and item["product_tmpl_id"]:
            tid = item["product_tmpl_id"][0] if isinstance(item["product_tmpl_id"], list) else item["product_tmpl_id"]
            by_template[tid] = item
        elif item["applied_on"] == "3_global":
            global_item = item
    return by_variant, by_template, global_item


def apply_price_rule(item, list_price):
    """Apply a pricelist.item rule to list_price and return the result."""
    if not item:
        return list_price
    cp = item.get("compute_price", "fixed")
    if cp == "fixed":
        return item["fixed_price"]
    elif cp == "percentage":
        return list_price * (1 - item["percent_price"] / 100)
    elif cp == "formula":
        return (list_price - item.get("price_discount", 0)) * (1 - item.get("price_surcharge", 0) / 100)
    return list_price


def get_price_for_product(p, tmpl_id, by_variant, by_template, global_item):
    """Cascade: variant rule -> template rule -> global rule -> list_price."""
    list_price = p["list_price"]
    if p["id"] in by_variant:
        return apply_price_rule(by_variant[p["id"]], list_price)
    elif tmpl_id in by_template:
        return apply_price_rule(by_template[tmpl_id], list_price)
    elif global_item:
        return apply_price_rule(global_item, list_price)
    return list_price


def get_all_packaging(models, uid):
    """Fetch ALL product.packaging in one call, grouped by tmpl_id.
    Returns {} if the model doesn't exist (Fault 2) — safe for GONDER Odoo SaaS.
    """
    try:
        packs = call(
            models, uid, "product.packaging", "search_read",
            [[]],
            {"fields": ["product_tmpl_id", "name", "qty"]}
        )
        result = {}
        for pk in packs:
            tmpl = pk.get("product_tmpl_id")
            if tmpl:
                tid = tmpl[0] if isinstance(tmpl, list) else tmpl
                result.setdefault(tid, []).append(pk)
        return result
    except Exception:
        return {}


def extract_m2_per_box(pkg_list):
    """From a list of packaging records, find the 'caja/box' entry and return (qty, name)."""
    for pk in pkg_list:
        pk_name_lower = (pk.get("name") or "").lower()
        if any(x in pk_name_lower for x in ["caja", "box", "paq", "pack"]):
            return pk.get("qty"), pk.get("name")
    return None, None


def send_telegram(message):
    """Fire-and-forget Telegram notification."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=5
        )
    except Exception:
        pass


# ── Rutas ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/pricelists")
def list_pricelists():
    """Debug: lista todas las tarifas disponibles en Odoo."""
    try:
        uid, models = get_odoo()
        result = call(models, uid, "product.pricelist", "search_read",
            [[]], {"fields": ["id", "name"]})
        return jsonify({"ok": True, "pricelists": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/search_product")
def search_product():
    """Busca un producto por codigo y devuelve precio para la tarifa solicitada."""
    code           = request.args.get("code", "").strip().upper()
    pricelist_name = request.args.get("pricelist", "USD BCV").strip()

    if not code:
        return jsonify({"ok": False, "error": "Codigo requerido"}), 400

    try:
        uid, models = get_odoo()

        # 1. Buscar variante por default_code
        variants = call(
            models, uid, "product.product", "search_read",
            [[["default_code", "=", code]]],
            {"fields": ["id", "name", "default_code", "list_price",
                        "product_tmpl_id", "image_128", "uom_id"], "limit": 1}
        )
        if not variants:
            return jsonify({"ok": False, "error": "Codigo no encontrado"}), 404

        p       = variants[0]
        tmpl_id = p["product_tmpl_id"][0] if isinstance(p.get("product_tmpl_id"), list) else p.get("product_tmpl_id")
        uom     = p["uom_id"][1] if isinstance(p.get("uom_id"), list) else ""

        # 2. Buscar tarifa por nombre (ilike = flexible a variaciones)
        pls = call(models, uid, "product.pricelist", "search_read",
            [[["name", "ilike", pricelist_name]]], {"fields": ["id"], "limit": 1})

        price = p["list_price"]
        if pls:
            pl_id  = pls[0]["id"]
            campos = {"fields": ["compute_price", "fixed_price", "percent_price",
                                 "price_discount", "price_surcharge"], "limit": 1}
            # Cascada: variante -> plantilla -> global
            for domain in [
                [["pricelist_id","=",pl_id], ["applied_on","=","0_product_variant"], ["product_id","=",p["id"]]],
                [["pricelist_id","=",pl_id], ["applied_on","=","1_product"],         ["product_tmpl_id","=",tmpl_id]],
                [["pricelist_id","=",pl_id], ["applied_on","=","3_global"]],
            ]:
                items = call(models, uid, "product.pricelist.item", "search_read", [domain], campos)
                if items:
                    price = apply_price_rule(items[0], p["list_price"])
                    break

        # 3. Packaging (m2/caja)
        pkg_list = []
        try:
            pkg_list = call(models, uid, "product.packaging", "search_read",
                [[["product_tmpl_id", "=", tmpl_id]]],
                {"fields": ["name", "qty"], "limit": 5})
        except Exception:
            pass

        m2_per_box, box_name = extract_m2_per_box(pkg_list)

        price_per_m2 = price_per_box = None
        if m2_per_box and m2_per_box > 0 and price > 0:
            uom_low = uom.lower()
            if "m" in uom_low and ("2" in uom_low or "\xb2" in uom_low):
                price_per_m2  = price
                price_per_box = price * m2_per_box
            else:
                price_per_box = price
                price_per_m2  = price / m2_per_box

        imagen = p.get("image_128")

        return jsonify({
            "ok": True,
            "product": {
                "variant_id":    p["id"],
                "tmpl_id":       tmpl_id,
                "code":          p["default_code"],
                "name":          p["name"],
                "uom":           uom,
                "price":         price,
                "image":         imagen if imagen else None,
                "price_per_m2":  price_per_m2,
                "price_per_box": price_per_box,
                "m2_per_box":    m2_per_box,
                "box_name":      box_name,
            }
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/catalog")
def catalog():
    """Devuelve todos los productos en stock con precios para ambas tarifas.
    Usa batch product.pricelist.item + cascada en Python — sin N+1 queries.
    """
    try:
        uid, models = get_odoo()

        # 1. IDs de ambas tarifas (2 llamadas)
        pl_ids = {}
        for name in PRICELIST_NAMES:
            pls = call(models, uid, "product.pricelist", "search_read",
                [[["name", "ilike", name]]], {"fields": ["id", "name"], "limit": 1})
            if pls:
                pl_ids[name] = pls[0]["id"]

        # 2. Todos los items de cada tarifa en batch (1 llamada por tarifa)
        pl_data = {}
        for name, pl_id in pl_ids.items():
            pl_data[name] = get_pricelist_items(models, uid, pl_id)

        # 3. Todos los productos con stock > 0 (1 llamada)
        productos = call(
            models, uid, "product.product", "search_read",
            [[["active", "=", True], ["default_code", "!=", False],
              ["sale_ok", "=", True], ["qty_available", ">", 0]]],
            {"fields": ["id", "name", "default_code", "list_price",
                        "product_tmpl_id", "image_128", "uom_id"],
             "order": "default_code asc"}
        )

        # 4. Todo el packaging en batch (1 llamada, try/except por si no existe)
        all_packaging = get_all_packaging(models, uid)

        # 5. Calcular precios y armar respuesta (puro Python, sin queries extra)
        result = []
        for p in productos:
            tmpl_id = p["product_tmpl_id"][0] if isinstance(p.get("product_tmpl_id"), list) else p.get("product_tmpl_id")
            uom     = p["uom_id"][1] if isinstance(p.get("uom_id"), list) else ""

            # Precio por tarifa (cascada Python, sin llamadas extra)
            prices = {}
            for name, (by_variant, by_template, global_item) in pl_data.items():
                prices[name] = get_price_for_product(p, tmpl_id, by_variant, by_template, global_item)

            # Packaging
            m2_per_box, box_packaging_name = extract_m2_per_box(all_packaging.get(tmpl_id, []))

            imagen = p.get("image_128")
            result.append({
                "id":                 tmpl_id,
                "variant_id":         p["id"],
                "code":               p["default_code"],
                "name":               p["name"],
                "uom":                uom,
                "prices":             prices,
                "image":              imagen if imagen else None,
                "m2_per_box":         m2_per_box,
                "box_packaging_name": box_packaging_name,
            })

        return jsonify({"ok": True, "products": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/product/<int:tmpl_id>")
def product_detail(tmpl_id):
    """Ficha tecnica completa de un producto (por template ID)."""
    try:
        uid, models = get_odoo()

        # 1. Leer plantilla
        tmpl = call(models, uid, "product.template", "read",
            [[tmpl_id]],
            {"fields": ["name", "default_code", "image_512", "uom_id",
                        "description_sale", "attribute_line_ids"]})
        if not tmpl:
            return jsonify({"ok": False, "error": "Producto no encontrado"}), 404
        t = tmpl[0]

        uom  = t["uom_id"][1] if isinstance(t.get("uom_id"), list) else ""
        code = t.get("default_code") or ""

        # Si la plantilla no tiene codigo, buscar en variante
        if not code:
            vars_ = call(models, uid, "product.product", "search_read",
                [[["product_tmpl_id", "=", tmpl_id], ["active", "=", True]]],
                {"fields": ["default_code"], "limit": 1})
            if vars_:
                code = vars_[0].get("default_code") or ""

        # 2. Atributos / especificaciones
        attrs = []
        if t.get("attribute_line_ids"):
            attr_lines = call(models, uid, "product.template.attribute.line", "read",
                [t["attribute_line_ids"]],
                {"fields": ["attribute_id", "value_ids"]})
            for line in attr_lines:
                attr_name = line["attribute_id"][1] if isinstance(line["attribute_id"], list) else str(line["attribute_id"])
                if line.get("value_ids"):
                    values = call(models, uid, "product.attribute.value", "read",
                        [line["value_ids"]], {"fields": ["name"]})
                    attrs.append({"attr": attr_name, "values": [v["name"] for v in values]})

        # 3. Packaging
        packaging = []
        try:
            packs = call(models, uid, "product.packaging", "search_read",
                [[["product_tmpl_id", "=", tmpl_id]]],
                {"fields": ["name", "qty"], "limit": 5})
            packaging = [{"name": pk["name"], "qty": pk["qty"]} for pk in packs]
        except Exception:
            pass

        imagen = t.get("image_512")

        return jsonify({
            "ok": True,
            "product": {
                "name":        t["name"],
                "code":        code,
                "uom":         uom,
                "description": t.get("description_sale") or "",
                "image":       imagen if imagen else None,
                "attributes":  attrs,
                "packaging":   packaging,
            }
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/create_order", methods=["POST"])
def create_order():
    """Crea el borrador de pedido en Odoo y notifica por Telegram."""
    data           = request.json or {}
    vendedor       = data.get("vendedor", "").strip()
    cliente        = data.get("cliente", "").strip()
    pricelist_name = data.get("pricelist", "USD BCV")
    nota           = data.get("nota", "")
    lines          = data.get("lines", [])

    if not vendedor:
        return jsonify({"ok": False, "error": "Vendedor requerido"}), 400
    if not cliente:
        return jsonify({"ok": False, "error": "Cliente requerido"}), 400
    if not lines:
        return jsonify({"ok": False, "error": "El pedido no tiene productos"}), 400

    try:
        uid, models = get_odoo()

        # 1. Buscar o crear partner
        partners = call(models, uid, "res.partner", "search",
            [[["name", "ilike", cliente]]], {"limit": 1})
        if partners:
            partner_id = partners[0]
        else:
            partner_id = call(models, uid, "res.partner", "create",
                [{"name": cliente, "customer_rank": 1}])

        # 2. Buscar tarifa
        pls = call(models, uid, "product.pricelist", "search_read",
            [[["name", "ilike", pricelist_name]]], {"fields": ["id"], "limit": 1})
        pricelist_id = pls[0]["id"] if pls else False

        # 3. Armar lineas
        order_lines = []
        subtotal    = 0.0
        for line in lines:
            order_lines.append((0, 0, {
                "product_id":      line["variant_id"],
                "name":            line["name"],
                "product_uom_qty": line["qty"],
                "price_unit":      line["price"],
            }))
            subtotal += line["qty"] * line["price"]

        # 4. Crear pedido
        order_vals = {
            "partner_id":       partner_id,
            "client_order_ref": vendedor,
            "order_line":       order_lines,
            "note":             nota,
        }
        if pricelist_id:
            order_vals["pricelist_id"] = pricelist_id

        order_id = call(models, uid, "sale.order", "create", [order_vals])

        # 5. Leer referencia generada
        order_data = call(models, uid, "sale.order", "read",
            [[order_id]], {"fields": ["name"]})
        order_name = order_data[0]["name"] if order_data else str(order_id)

        # 6. Notificacion Telegram
        lines_text = "\n".join(
            f"  - {l['code']} x{l['qty']} @ {l['price']:.2f}" for l in lines
        )
        msg = (
            f"<b>Nuevo Pedido GONDER</b>\n"
            f"<b>Vendedor:</b> {vendedor}\n"
            f"<b>Cliente:</b> {cliente}\n"
            f"<b>Tarifa:</b> {pricelist_name}\n"
            f"<b>Referencia:</b> {order_name}\n"
            f"<b>Total:</b> {subtotal:,.2f}\n\n"
            f"<b>Productos:</b>\n{lines_text}"
        )
        if nota:
            msg += f"\n\n<b>Nota:</b> {nota}"
        send_telegram(msg)

        return jsonify({"ok": True, "order_name": order_name})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)
