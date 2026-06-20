import os
import xmlrpc.client
import requests
from flask import Flask, request, jsonify, render_template

app = Flash(__name__)

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
        disc     = item.get("price_discount", 0.0)
        surcharge = item.get("price_surcharge", 0.0)
        return base_price * (1.0 - disc / 100.0) + surcharge
    return base_price


def get_price_for_product(variant_id, tmpl_id, categ_chain, list_price,
                           by_variant, by_template, by_category, global_item,
                           base_pl_price=None):
    """Cascada Odoo: variante â template â categoria (jerarquia) â global â list_price.
    base_pl_price: precio ya calculado de otra tarifa base (para reglas base='pricelist').
    """
    # Encontrar la regla que aplica
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

    # Precio base segÃºn configuraciÃ³n de la regla
    base = item.get("base", "list_price")
    if base == "pricelist" and base_pl_price is not None:
        bp = base_pl_price
    else:
        bp = list_price

    return apply_price_rule(item, bp)


def build_categ_chains(models, uid, categ_ids):
    """Para un conjunto de categ_ids, construye {categ_id: [cid, parent, grandparent, ...]}.
    Un fetch de todas las categorÃ­as evita N+1 queries.
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


@app.route("/api/debug_price")
def debug_price():
    """Debug completo: price_get, campos custom, todos los items de la tarifa."""
    code = request.args.get("code", "").strip().upper()
    if not code:
        return jsonify({"ok": False, "error": "code requerido"}), 400
    try:
        uid, models = get_odoo()

        # Producto
        variants = call(models, uid, "product.product", "search_read",
            [[["default_code", "=", code]]],
            {"fields": ["id", "name", "list_price", "standard_price",
                        "product_tmpl_id", "uom_id"], "limit": 1})
        if not variants:
            return jsonify({"ok": False, "error": "Producto no encontrado"})
        p = variants[0]
        tmpl_id = p["product_tmpl_id"][0] if isinstance(p["product_tmpl_id"], list) else p["product_tmpl_id"]

        # Campos custom (x_*) del producto template
        all_fields = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD,
            "product.template", "fields_get", [],
            {"attributes": ["string", "type"]})
        custom_fields = {k: v for k, v in all_fields.items() if k.startswith("x_")}

        custom_vals = {}
        if custom_fields:
            tmpl_data = call(models, uid, "product.template", "read",
                [[tmpl_id]], {"fields": list(custom_fields.keys())})
            if tmpl_data:
                custom_vals = {k: tmpl_data[0].get(k) for k in custom_fields}

        # Tarifas
        pricelists = call(models, uid, "product.pricelist", "search_read",
            [[]], {"fields": ["id", "name", "currency_id"]})

        # Para cada tarifa: price_get, get_product_price, todos los items
        price_results = {}
        for pl in pricelists:
            pl_id = pl["id"]
            entry = {"currency": pl.get("currency_id"), "price_get": None,
                     "get_product_price": None, "all_items_count": 0}

            try:
                r = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD,
                    "product.pricelist", "price_get",
                    [[pl_id], p["id"], 1.0])
                entry["price_get"] = r
            except Exception as e:
                entry["price_get"] = f"ERROR: {e}"

            try:
                r2 = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD,
                    "product.pricelist", "get_product_price",
                    [[pl_id], p["id"], 1.0, False])
                entry["get_product_price"] = r2
            except Exception as e:
                entry["get_product_price"] = f"ERROR: {e}"

            all_items = call(models, uid, "product.pricelist.item", "search_read",
                [[["pricelist_id", "=", pl_id]]],
                {"fields": ["applied_on", "compute_price", "fixed_price",
                            "percent_price", "price_discount", "price_surcharge",
                            "base", "base_pricelist_id"]})
            entry["all_items_count"] = len(all_items)
            entry["all_items"] = all_items

            price_results[pl["name"]] = entry

        return jsonify({
            "product": p, "tmpl_id": tmpl_id,
            "custom_fields": custom_fields,
            "custom_values": custom_vals,
            "price_results": price_results
        })
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
                        "product_tmpl_id", "image_128", "uom_id", "categ_id"], "limit": 1}
        )
        if not variants:
            return jsonify({"ok": False, "error": "Codigo no encontrado"}), 404

        p       = variants[0]
        tmpl_id = p["product_tmpl_id"][0] if isinstance(p.get("product_tmpl_id"), list) else p.get("product_tmpl_id")
        uom     = p["uom_id"][1] if isinstance(p.get("uom_id"), list) else ""

        # 2. Cascada manual de precios (Odoo 19 no tiene price_get ni get_product_price)
        categ_raw = p.get("categ_id")
        categ_id  = categ_raw[0] if isinstance(categ_raw, list) else categ_raw
        categ_chain = build_categ_chains(models, uid, {categ_id} if categ_id else {}).get(categ_id, [])

        # Cargar items de la tarifa solicitada + USD (puede ser base)
        pls_all = call(models, uid, "product.pricelist", "search_read",
            [[]], {"fields": ["id", "name"]})
        pl_map = {pl["name"]: pl["id"] for pl in pls_all}

        # IDs de tarifas base posibles
        usd_pl_id = pl_map.get("USD") or pl_map.get("USD (USD)")
        target_pl_id = None
        for n, i in pl_map.items():
            if pricelist_name.lower() in n.lower():
                target_pl_id = i
                break

        price = p["list_price"]
        if target_pl_id:
            bv, bt, bc, gi = get_pricelist_items(models, uid, target_pl_id)
            # Precio USD como base (por si la tarifa referencia a USD)
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
    """Devuelve todos los productos en stock con precios para ambas tarifas.
    Usa batch product.pricelist.item + cascada en Python â sin N+1 queries.
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

        # 2. Todos los productos con stock > 0 (1 llamada)
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

        # 4. JerarquÃ­a de categorÃ­as para todos los productos (1 fetch de todas las cats)
        all_categ_ids = set()
        for p in productos:
            cid = p.get("categ_id")
            if cid:
                all_categ_ids.add(cid[0] if isinstance(cid, list) else cid)
        categ_chains = build_categ_chains(models, uid, all_categ_ids)

        # USD se usa como base para reglas USD BCV con base='pricelist'
        usd_items = pl_items.get("USD")

        # 5. Todo el packaging en batch (1 llamada, try/except por si no existe)
        all_packaging = get_all_packaging(models, uid)

        # 6. Armar respuesta
        result = []
        for p in productos:
            tmpl_id = p["product_tmpl_id"][0] if isinstance(p.get("product_tmpl_id"), list) else p.get("product_tmpl_id")
            uom     = p["uom_id"][1] if isinstance(p.get("uom_id"), list) else ""

            categ_raw = p.get("categ_id")
            categ_id  = categ_raw[0] if isinstance(categ_raw, list) else categ_raw
            categ_chain = categ_chains.get(categ_id, [categ_id] if categ_id else [])

            # Calcular USD primero (puede ser base para USD BCV)
            usd_price = p["list_price"]
            if usd_items:
                bv, bt, bc, gi = usd_items
                usd_price = get_price_for_product(
                    p["id"], tmpl_id, categ_chain, p["list_price"], bv, bt, bc, gi)

            # Calcular resto de tarifas
            prices = {}
            for name, items_tuple in pl_items.items():
                bv, bt, bc, gi = items_tuple
                if name == "USD":
                    prices[name] = usd_price
     
