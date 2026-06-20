import os
import xmlrpc.client
import requests
from flask import Flask, request, jsonify, render_template

app = Flask(__name__)

# ââ Credenciales Odoo (desde variables de entorno) âââââââââââââââââââââââââ
ODOO_URL      = os.environ.get("ODOO_URL",      "https://gonder.odoo.com")
ODOO_DB       = os.environ.get("ODOO_DB",       "gonder")
ODOO_USER     = os.environ.get("ODOO_USER",     "")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD", "")

# ââ Telegram ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",   "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ââ Nombres de tarifas ââââââââââââââââââââââââââââââââââââââââââââââââââââââ
PRICELIST_NAMES = ["USD BCV", "USD"]


# ââ Helpers Odoo âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

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
    """Fetch ALL pricelist.item records para una tarifa.
    Retorna (by_variant, by_template, by_category, global_item).
    Odoo 19 no tiene price_get ni get_product_price â usamos cascada manual.
    """
    items = call(
        models, uid, "product.pricelist.item", "search_read",
        [[["pricelist_id", "=", pl_id]]],
        {"fields": ["applied_on", "product_id", "product_tmpl_id", "categ_id",
                    "compute_price", "fixed_price", "percent_price",
                    "price_discount", "price_surcharge", "base", "base_pricelist_id"]}
    )
    by_variant  = {}
    by_template = {}
    by_category = {}
    global_item = None
    for item in items:
        ao = item["applied_on"]
        if ao == "0_product_variant" and item.get("product_id"):
            pid = item["product_id"][0] if isinstance(item["product_id"], list) else item["product_id"]
            by_variant.setdefault(pid, item)
        elif ao == "1_product" and item.get("product_tmpl_id"):
            tid = item["product_tmpl_id"][0] if isinstance(item["product_tmpl_id"], list) else item["product_tmpl_id"]
            by_template.setdefault(tid, item)
        elif ao == "2_product_category" and item.get("categ_id"):
            cid = item["categ_id"][0] if isinstance(item["categ_id"], list) else item["categ_id"]
            by_category.setdefault(cid, item)
        elif ao == "3_global":
            if global_item is None:
                global_item = item
    return by_variant, by_template, by_category, global_item


def apply_price_rule(item, base_price):
    """Aplica una regla de tarifa sobre base_price."""
    if not item:
        return base_price
    cp = item.get("compute_price", "fixed")
    if cp == "fixed":
        return float(item.get("fixed_price", base_price))
    elif cp == "percentage":
        # percent_price negativo = markup. Ej: -40 â price = base * 1.40
        pct = item.get("percent_price", 0.0)
        return base_price * (1.0 - pct / 100.0)
    elif cp == "formula":
        disc      = item.get("price_discount", 0.0)
        surcharge = item.get("price_surcharge", 0.0)
        return base_price * (1.0 - disc / 100.0) + surcharge
    return base_price


def get_price_for_product(variant_id, tmpl_id, categ_chain, list_price,
                           by_variant, by_template, by_category, global_item,
                           base_pl_price=None):
    """Cascada Odoo: variante â template â categoria (jerarquia) â global â list_price.
    base_pl_price: precio ya calculado de otra tarifa base (para reglas base='pricelist').
    """
    if variant_id in by_variant:
        item = by_variant[variant_id]
    elif tmpl_id in by_template:
        item = by_template[tmpl_id]
    else:
        item = None
        for cid in categ_chain:
            if cid in by_category:
                item = by_category[cid]
                break
        if item is None:
            item = global_item

    if item is None:
        return list_price

    base = item.get("base", "list_price")
    if base == "pricelist" and base_pl_price is not None:
        bp = base_pl_price
    else:
        bp = list_price

    return apply_price_rule(item, bp)


def find_pricelist_by_name(pls_all, target_name, exclude_ids=None):
    """Encuentra la tarifa que mejor corresponde a target_name.
    Evita que "USD" matchee "USD BCV" usando prioridades:
    1. Exact match (case-insensitive)
    2. Nombre empieza con target + espacio o parentesis (ej "USD (USD)" para "USD")
    3. Contiene target (ultimo recurso)
    exclude_ids: IDs ya usados para evitar doble-match.
    """
    exclude_ids = exclude_ids or set()
    tl = target_name.lower()
    for pl in pls_all:
        if pl["id"] not in exclude_ids and pl["name"].lower() == tl:
            return pl
    for pl in pls_all:
        if pl["id"] not in exclude_ids:
            nl = pl["name"].lower()
            if nl.startswith(tl + " ") or nl.startswith(tl + "("):
                return pl
    for pl in pls_all:
        if pl["id"] not in exclude_ids and tl in pl["name"].lower():
            return pl
    return None


def build_categ_chains(models, uid, categ_ids):
    """Para un conjunto de categ_ids, construye {categ_id: [cid, parent, grandparent, ...]}.
    Un fetch de todas las categorias evita N+1 queries.
    """
    if not categ_ids:
        return {}
    cats = call(models, uid, "product.category", "search_read",
        [[]], {"fields": ["id", "parent_id"]})
    cat_map = {c["id"]: c for c in cats}

    result = {}
    for start_cid in categ_ids:
        chain = []
        current = start_cid
        seen = set()
        while current and current not in seen:
            seen.add(current)
            chain.append(current)
            parent = cat_map.get(current, {}).get("parent_id")
            current = parent[0] if isinstance(parent, list) and parent else None
        result[start_cid] = chain
    return result


def get_all_packaging(models, uid):
    """Fetch ALL product.packaging in one call, grouped by tmpl_id.
    Returns {} if the model doesn't exist (Fault 2) â safe for GONDER Odoo SaaS.
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


# ââ Rutas âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

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


@app.route("/api/debug_cascade")
def debug_cascade():
    """Debug de cascada: muestra que tarifa matchea, categ_chain, y precio calculado."""
    code = request.args.get("code", "").strip().upper()
    if not code:
        return jsonify({"ok": False, "error": "code requerido"}), 400
    try:
        uid, models = get_odoo()

        variants = call(models, uid, "product.product", "search_read",
            [[["default_code", "=", code]]],
            {"fields": ["id", "name", "list_price", "product_tmpl_id", "categ_id"], "limit": 1})
        if not variants:
            return jsonify({"ok": False, "error": "Producto no encontrado"}), 404
        p = variants[0]
        tmpl_id   = p["product_tmpl_id"][0] if isinstance(p["product_tmpl_id"], list) else p["product_tmpl_id"]
        categ_raw = p.get("categ_id")
        categ_id  = categ_raw[0] if isinstance(categ_raw, list) else categ_raw
        categ_name = categ_raw[1] if isinstance(categ_raw, list) else ""

        categ_chain_map = build_categ_chains(models, uid, {categ_id} if categ_id else {})
        categ_chain = categ_chain_map.get(categ_id, [])

        pls_all = call(models, uid, "product.pricelist", "search_read",
            [[]], {"fields": ["id", "name"]})
        used_ids = set()
        matched_pls = {}
        for name in sorted(PRICELIST_NAMES, key=len, reverse=True):
            m = find_pricelist_by_name(pls_all, name, used_ids)
            if m:
                matched_pls[name] = {"id": m["id"], "odoo_name": m["name"]}
                used_ids.add(m["id"])

        results = {}
        usd_price = p["list_price"]
        usd_key = next((n for n in PRICELIST_NAMES if n.upper() == "USD"), None)

        for name, pl_info in matched_pls.items():
            bv, bt, bc, gi = get_pricelist_items(models, uid, pl_info["id"])
            bc_keys = list(bc.keys())

            if p["id"] in bv:
                rule_type = f"variant:{p['id']}"
            elif tmpl_id in bt:
                rule_type = f"template:{tmpl_id}"
            else:
                matched_cid = next((cid for cid in categ_chain if cid in bc), None)
                if matched_cid:
                    rule_type = f"category:{matched_cid}"
                elif gi:
                    rule_type = "global"
                else:
                    rule_type = "none->list_price"

            price = get_price_for_product(p["id"], tmpl_id, categ_chain,
                                          p["list_price"], bv, bt, bc, gi,
                                          base_pl_price=(usd_price if name != usd_key else None))
            if name == usd_key:
                usd_price = price

            results[name] = {
                "pl_id": pl_info["id"],
                "odoo_name": pl_info["odoo_name"],
                "by_category_keys": bc_keys,
                "rule_matched": rule_type,
                "categ_chain_intersect": [c for c in categ_chain if c in bc],
                "price": price,
            }

        return jsonify({
            "product_id": p["id"],
            "tmpl_id": tmpl_id,
            "list_price": p["list_price"],
            "categ_id": categ_id,
            "categ_name": categ_name,
            "categ_chain": categ_chain,
            "all_pricelists": [{"id": pl["id"], "name": pl["name"]} for pl in pls_all],
            "pricelist_results": results,
        })
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e), "trace": traceback.format_exc()}), 500


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
                        "product_tmpl_id", "image_128", "uom_id", "categ_id"], "limit": 1}
        )
        if not variants:
            return jsonify({"ok": False, "error": "Codigo no encontrado"}), 404

        p       = variants[0]
        tmpl_id = p["product_tmpl_id"][0] if isinstance(p.get("product_tmpl_id"), list) else p.get("product_tmpl_id")
        uom     = p["uom_id"][1] if isinstance(p.get("uom_id"), list) else ""

        # 2. Cascada manual de precios (Odoo 19 no tiene price_get)
        categ_raw = p.get("categ_id")
        categ_id  = categ_raw[0] if isinstance(categ_raw, list) else categ_raw
        categ_chain = build_categ_chains(models, uid, {categ_id} if categ_id else {}).get(categ_id, [])

        pls_all = call(models, uid, "product.pricelist", "search_read",
            [[]], {"fields": ["id", "name"]})

        # Match exacto primero (evita "USD" matchear "USD BCV")
        target_pl = find_pricelist_by_name(pls_all, pricelist_name)
        target_pl_id = target_pl["id"] if target_pl else None

        # Buscar USD excluyendo la tarifa ya encontrada
        usd_pl = find_pricelist_by_name(pls_all, "USD",
                                         exclude_ids={target_pl_id} if target_pl_id else set())
        usd_pl_id = usd_pl["id"] if usd_pl else None

        price = p["list_price"]
        if target_pl_id:
            bv, bt, bc, gi = get_pricelist_items(models, uid, target_pl_id)
            usd_price = None
            if usd_pl_id and usd_pl_id != target_pl_id:
                bv_u, bt_u, bc_u, gi_u = get_pricelist_items(models, uid, usd_pl_id)
                usd_price = get_price_for_product(
                    p["id"], tmpl_id, categ_chain, p["list_price"],
                    bv_u, bt_u, bc_u, gi_u)
            price = get_price_for_product(
                p["id"], tmpl_id, categ_chain, p["list_price"],
                bv, bt, bc, gi, base_pl_price=usd_price)

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
    """Devuelve todos los productos en stock con precios para ambas tarifas."""
    try:
        uid, models = get_odoo()

        # 1. IDs de ambas tarifas (match exacto â evita "USD" â "USD BCV")
        pls_all_cat = call(models, uid, "product.pricelist", "search_read",
            [[]], {"fields": ["id", "name"]})
        pl_ids = {}
        used_ids = set()
        for name in sorted(PRICELIST_NAMES, key=len, reverse=True):
            match = find_pricelist_by_name(pls_all_cat, name, used_ids)
            if match:
                pl_ids[name] = match["id"]
                used_ids.add(match["id"])

        # 2. Todos los productos con stock > 0
        productos = call(
            models, uid, "product.product", "search_read",
            [[["active", "=", True], ["default_code", "!=", False],
              ["sale_ok", "=", True], ["qty_available", ">", 0]]],
            {"fields": ["id", "name", "default_code", "list_price",
                        "product_tmpl_id", "image_128", "uom_id", "categ_id"],
             "order": "default_code asc"}
        )

        # 3. Items de cada tarifa (cascada manual â Odoo 19 no tiene price_get)
        pl_items = {}
        for name, pl_id in pl_ids.items():
            pl_items[name] = get_pricelist_items(models, uid, pl_id)

        # 4. Jerarquia de categorias (1 fetch de todas las cats)
        all_categ_ids = set()
        for p in productos:
            cid = p.get("categ_id")
            if cid:
                all_categ_ids.add(cid[0] if isinstance(cid, list) else cid)
        categ_chains = build_categ_chains(models, uid, all_categ_ids)

        usd_items = pl_items.get("USD")
        all_packaging = get_all_packaging(models, uid)

        result = []
        for p in productos:
            tmpl_id = p["product_tmpl_id"][0] if isinstance(p.get("product_tmpl_id"), list) else p.get("product_tmpl_id")
            uom     = p["uom_id"][1] if isinstance(p.get("uom_id"), list) else ""

            categ_raw = p.get("categ_id")
            categ_id  = categ_raw[0] if isinstance(categ_raw, list) else categ_raw
            categ_chain = categ_chains.get(categ_id, [categ_id] if categ_id else [])

            usd_price = p["list_price"]
            if usd_items:
                bv, bt, bc, gi = usd_items
                usd_price = get_price_for_product(
                    p["id"], tmpl_id, categ_chain, p["list_price"], bv, bt, bc, gi)

            prices = {}
            for name, items_tuple in pl_items.items():
                bv, bt, bc, gi = items_tuple
                if name == "USD":
                    prices[name] = usd_price
                else:
                    prices[name] = get_price_for_product(
                        p["id"], tmpl_id, categ_chain, p["list_price"],
                        bv, bt, bc, gi, base_pl_price=usd_price)

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

        tmpl = call(models, uid, "product.template", "read",
            [[tmpl_id]],
            {"fields": ["name", "default_code", "image_512", "uom_id",
                        "description_sale", "attribute_line_ids"]})
        if not tmpl:
            return jsonify({"ok": False, "error": "Producto no encontrado"}), 404
        t = tmpl[0]

        uom  = t["uom_id"][1] if isinstance(t.get("uom_id"), list) else ""
        code = t.get("default_code") or ""

        if not code:
            vars_ = call(models, uid, "product.product", "search_read",
                [[["product_tmpl_id", "=", tmpl_id], ["active", "=", True]]],
                {"fields": ["default_code"], "limit": 1})
            if vars_:
                code = vars_[0].get("default_code") or ""

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

        partners = call(models, uid, "res.partner", "search",
            [[["name", "ilike", cliente]]], {"limit": 1})
        if partners:
            partner_id = partners[0]
        else:
            partner_id = call(models, uid, "res.partner", "create",
                [{"name": cliente, "customer_rank": 1}])

        pls = call(models, uid, "product.pricelist", "search_read",
            [[["name", "ilike", pricelist_name]]], {"fields": ["id"], "limit": 1})
        pricelist_id = pls[0]["id"] if pls else False

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

        order_vals = {
            "partner_id":       partner_id,
            "client_order_ref": vendedor,
            "order_line":       order_lines,
            "note":             nota,
        }
        if pricelist_id:
            order_vals["pricelist_id"] = pricelist_id

        order_id = call(models, uid, "sale.order", "create", [order_vals])

        order_data = call(models, uid, "sale.order", "read",
            [[order_id]], {"fields": ["name"]})
        order_name = order_data[0]["name"] if order_data else str(order_id)

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
