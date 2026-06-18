import os
import xmlrpc.client
import requests
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

ODOO_URL      = os.environ.get("ODOO_URL", "https://gonder.odoo.com")
ODOO_DB       = os.environ.get("ODOO_DB", "gonder")
ODOO_USER     = os.environ.get("ODOO_USER", "")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD", "")

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

PRICELISTS = ["USD BCV", "USD"]


def odoo_connect():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        raise ConnectionError("No se pudo autenticar en Odoo.")
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return uid, models


def get_pricelist_id(models, uid, pricelist_name):
    result = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.pricelist", "search_read",
        [[["name", "=", pricelist_name]]],
        {"fields": ["id", "name"], "limit": 1}
    )
    return result[0]["id"] if result else None


def get_price_from_pricelist(models, uid, pricelist_id, product_id, qty=1):
    result = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.pricelist", "price_get",
        [pricelist_id, product_id, qty]
    )
    return result.get(str(pricelist_id), result.get(pricelist_id, 0))


def get_packaging(models, uid, product_tmpl_id):
    packaging = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.packaging", "search_read",
        [[["product_tmpl_id", "=", product_tmpl_id]]],
        {"fields": ["id", "name", "qty", "product_uom_id"], "limit": 5}
    )
    return packaging


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/catalog")
def catalog():
    try:
        uid, models = odoo_connect()
        pricelist_ids = {}
        for name in PRICELISTS:
            pid = get_pricelist_id(models, uid, name)
            if pid:
                pricelist_ids[name] = pid
        products = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "product.template", "search_read",
            [[["sale_ok", "=", True], ["active", "=", True]]],
            {"fields": ["id", "name", "default_code", "image_128", "uom_id", "description_sale", "attribute_line_ids"], "limit": 500}
        )
        catalog_items = []
        for p in products:
            variants = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "product.product", "search_read",
                [[["product_tmpl_id", "=", p["id"]], ["active", "=", True]]],
                {"fields": ["id", "default_code"], "limit": 1}
            )
            if not variants:
                continue
            variant_id = variants[0]["id"]
            prices = {}
            for pl_name, pl_id in pricelist_ids.items():
                prices[pl_name] = get_price_from_pricelist(models, uid, pl_id, variant_id)
            uom_name = p["uom_id"][1] if p.get("uom_id") else ""
            packaging = get_packaging(models, uid, p["id"])
            m2_per_box = None
            box_packaging = None
            for pk in packaging:
                pk_name_lower = (pk.get("name") or "").lower()
                if any(x in pk_name_lower for x in ["caja", "box", "paq", "pack"]):
                    box_packaging = pk
                    m2_per_box = pk.get("qty")
                    break
            item = {
                "id": p["id"], "variant_id": variant_id,
                "name": p["name"], "code": p.get("default_code") or variants[0].get("default_code") or "",
                "image": p.get("image_128") or "", "uom": uom_name, "prices": prices,
                "m2_per_box": m2_per_box, "box_packaging_name": box_packaging["name"] if box_packaging else None,
            }
            catalog_items.append(item)
        return jsonify({"ok": True, "products": catalog_items})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/product/<int:product_tmpl_id>")
def product_detail(product_tmpl_id):
    try:
        uid, models = odoo_connect()
        product = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "product.template", "read", [product_tmpl_id],
            {"fields": ["id", "name", "default_code", "image_1920", "description_sale", "uom_id", "attribute_line_ids", "product_variant_ids"]}
        )
        if not product:
            return jsonify({"ok": False, "error": "Producto no encontrado"}), 404
        p = product[0]
        attrs = []
        if p.get("attribute_line_ids"):
            attr_lines = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "product.template.attribute.line", "read", [p["attribute_line_ids"]], {"fields": ["attribute_id", "value_ids"]})
            for line in attr_lines:
                attr_name = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "product.attribute", "read", [line["attribute_id"][0]], {"fields": ["name"]})[0]["name"]
                values = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "product.attribute.value", "read", [line["value_ids"]], {"fields": ["name"]})
                attrs.append({"attr": attr_name, "values": [v["name"] for v in values]})
        packaging = get_packaging(models, uid, product_tmpl_id)
        return jsonify({"ok": True, "product": {"id": p["id"], "name": p["name"], "code": p.get("default_code") or "", "image": p.get("image_1920") or "", "uom": p["uom_id"][1] if p.get("uom_id") else "", "description": p.get("description_sale") or "", "attributes": attrs, "packaging": packaging}})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/search_product")
def search_product():
    code = request.args.get("code", "").strip()
    pricelist_name = request.args.get("pricelist", PRICELISTS[0])
    if not code:
        return jsonify({"ok": False, "error": "Codigo vacio"}), 400
    try:
        uid, models = odoo_connect()
        variants = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "product.product", "search_read", [[["default_code", "=ilike", code], ["active", "=", True]]], {"fields": ["id", "name", "default_code", "product_tmpl_id", "image_128", "uom_id"], "limit": 1})
        if not variants:
            templates = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "product.template", "search_read", [[["default_code", "=ilike", code], ["active", "=", True]]], {"fields": ["id", "name", "default_code", "uom_id"], "limit": 1})
            if not templates:
                return jsonify({"ok": False, "error": f"Producto '{code}' no encontrado"}), 404
            t = templates[0]
            variant_list = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "product.product", "search_read", [[["product_tmpl_id", "=", t["id"]], ["active", "=", True]]], {"fields": ["id", "image_128"], "limit": 1})
            variant_id = variant_list[0]["id"] if variant_list else None
            image = variant_list[0].get("image_128") if variant_list else None
            tmpl_id = t["id"]; name = t["name"]; uom = t["uom_id"][1] if t.get("uom_id") else ""
        else:
            v = variants[0]; variant_id = v["id"]; tmpl_id = v["product_tmpl_id"][0]; name = v["name"]; image = v.get("image_128"); uom = v["uom_id"][1] if v.get("uom_id") else ""
        pl_id = get_pricelist_id(models, uid, pricelist_name)
        price = get_price_from_pricelist(models, uid, pl_id, variant_id) if pl_id and variant_id else 0
        packaging = get_packaging(models, uid, tmpl_id)
        m2_per_box = None; box_name = None
        for pk in packaging:
            if any(x in (pk.get("name") or "").lower() for x in ["caja", "box", "paq", "pack"]):
                m2_per_box = pk.get("qty"); box_name = pk.get("name"); break
        price_per_m2 = None; price_per_box = None
        if m2_per_box and m2_per_box > 0 and price > 0:
            if uom.lower() in ["m2", "metro cuadrado"]:
                price_per_m2 = price; price_per_box = price * m2_per_box
            else:
                price_per_box = price; price_per_m2 = price / m2_per_box
        return jsonify({"ok": True, "product": {"variant_id": variant_id, "tmpl_id": tmpl_id, "name": name, "code": code, "image": image or "", "uom": uom, "price": price, "price_per_m2": price_per_m2, "price_per_box": price_per_box, "m2_per_box": m2_per_box, "box_name": box_name, "pricelist": pricelist_name}})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/create_order", methods=["POST"])
def create_order():
    data = request.get_json()
    vendedor = data.get("vendedor", ""); cliente = data.get("cliente", "")
    pricelist = data.get("pricelist", PRICELISTS[0]); lines = data.get("lines", []); nota = data.get("nota", "")
    if not lines:
        return jsonify({"ok": False, "error": "Sin lineas de pedido"}), 400
    try:
        uid, models = odoo_connect()
        pl_id = get_pricelist_id(models, uid, pricelist)
        partner_ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "search", [[["name", "ilike", cliente]]], {"limit": 1})
        partner_id = partner_ids[0] if partner_ids else models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "create", [{"name": cliente, "customer_rank": 1}])
        order_lines = [(0, 0, {"product_id": l["variant_id"], "product_uom_qty": l["qty"], "price_unit": l["price"]}) for l in lines]
        note_parts = [f"Vendedor: {vendedor}"]
        if nota: note_parts.append(nota)
        order_vals = {"partner_id": partner_id, "order_line": order_lines, "note": "\n".join(note_parts), "state": "draft"}
        if pl_id: order_vals["pricelist_id"] = pl_id
        order_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "sale.order", "create", [order_vals])
        order_name = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "sale.order", "read", [order_id], {"fields": ["name"]})[0]["name"]
        if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
            _send_telegram(vendedor, cliente, pricelist, lines, order_name)
        return jsonify({"ok": True, "order_id": order_id, "order_name": order_name})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _send_telegram(vendedor, cliente, pricelist, lines, order_name):
    total = sum(l["price"] * l["qty"] for l in lines)
    msg = f"Nuevo Pedido GONDER Sucursal - {order_name}\nVendedor: {vendedor}\nCliente: {cliente}\nTarifa: {pricelist}\n"
    for l in lines:
        msg += f"  [{l['code']}] {l['name']} x{l['qty']} @ {l['price']:.2f}\n"
    msg += f"Total: {total:.2f}"
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=5)
    except Exception:
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
