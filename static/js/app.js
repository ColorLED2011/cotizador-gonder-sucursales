/**
 * Cotizador Sucursales GONDER
 * Frontend SPA — Carrito en memoria + html5-qrcode + Fetch API
 */

(function () {
  'use strict';

  // ── Constantes ───────────────────────────────────────────────────────────────
  const CONFIG_KEY  = 'gonder_config';
  const DEBOUNCE_MS = 350;

  // ── Estado global ────────────────────────────────────────────────────────────
  let carrito      = [];
  let tasaBCV      = 0;
  let config       = { plEstandar: null, plBCV: null };
  let scanner      = null;
  let scannerMode  = 'cart';   // 'cart' | 'catalog'
  let clienteId    = null;
  let catProductos = [];       // catálogo completo para filtrar localmente
  let buscarTimer  = null;
  let clienteTimer = null;

  // ── Caché de productos (para evitar pasar JSON en onclick) ───────────────────
  const _prodCache    = {};   // id → producto (buscador / catálogo)
  const _clienteCache = {};   // id → cliente
  let   _fichaActual  = null; // producto que está en la ficha ahora mismo

  // ── Helpers ──────────────────────────────────────────────────────────────────
  const fUSD = n => '$ ' + Number(n).toFixed(2);
  const fBs  = n => 'Bs. ' + Number(n).toLocaleString('es-VE', {
    minimumFractionDigits: 2, maximumFractionDigits: 2
  });
  const clamp = n => Math.max(0.01, Math.round(n * 100) / 100);
  const el    = id  => document.getElementById(id);

  function escHtml(str) {
    return String(str || '')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function toast(msg, color) {
    const t = el('toast');
    t.textContent    = msg;
    t.style.background = color || '#1e293b';
    t.classList.add('show');
    setTimeout(() => t.classList.remove('show'), 2500);
  }

  // ── Parámetros de lista de precio ────────────────────────────────────────────
  function plParams(leading = '&') {
    const p = [];
    if (config.plEstandar) p.push(`pl_estandar=${config.plEstandar}`);
    if (config.plBCV)      p.push(`pl_bcv=${config.plBCV}`);
    return p.length ? leading + p.join('&') : '';
  }

  // ── Navegación ───────────────────────────────────────────────────────────────
  function goTo(screenId) {
    document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
    el(screenId).classList.add('active');
    if (screenId === 'scr-catalogo' && catProductos.length === 0) cargarCatalogo();
  }

  // ── Inicialización ───────────────────────────────────────────────────────────
  function init() {
    const saved = localStorage.getItem(CONFIG_KEY);
    if (saved) {
      try { config = JSON.parse(saved); } catch { config = { plEstandar: null, plBCV: null }; }
    }
    // Arrancar la app inmediatamente; listas de precio se cargan en segundo plano
    mostrarApp();
    _cargarListasFondo();
  }

  async function _cargarListasFondo() {
    if (config.plEstandar && config.plBCV) return;
    try {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 8000);
      const resp = await fetch('/api/listas-precio', { signal: ctrl.signal }).then(x => x.json());
      clearTimeout(timer);
      if (resp.error || !resp.listas?.length) return;
      if (!config.plEstandar) config.plEstandar = resp.listas[0].id;
      if (!config.plBCV)      config.plBCV      = resp.listas[0].id;
      localStorage.setItem(CONFIG_KEY, JSON.stringify(config));
    } catch { /* sin conexion Odoo */ }
  }

  function mostrarApp() {
    el('modal-config').classList.add('hidden');
    el('app').classList.remove('hidden');
    cargarTasa();
  }

  // ── Tasa BCV ─────────────────────────────────────────────────────────────────
  async function cargarTasa() {
    try {
      const d = await fetch('/api/tasa').then(r => r.json());
      if (d.tasa) {
        tasaBCV = d.tasa;
        el('hdr-tasa').textContent = fBs(d.tasa);
        actualizarTotales();
      }
    } catch { /* mantener último valor */ }
  }

  // ── Configuración ─────────────────────────────────────────────────────────────
  async function mostrarConfig() {
    el('modal-config').classList.remove('hidden');
    el('app').classList.add('hidden');
    el('config-loading').classList.remove('hidden');
    el('config-form').classList.add('hidden');
    el('config-error-main').classList.add('hidden');

    try {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 10000);
      const d = await fetch('/api/listas-precio', { signal: ctrl.signal }).then(r => r.json());
      clearTimeout(timer);
      if (d.error) throw new Error(d.error);

      const opts = d.listas.map(l =>
        `<option value="${l.id}">${escHtml(l.name)} (${escHtml(l.currency_id[1])})</option>`
      ).join('');

      el('sel-estandar').innerHTML = opts;
      el('sel-bcv').innerHTML      = opts;
      if (config.plEstandar) el('sel-estandar').value = config.plEstandar;
      if (config.plBCV)      el('sel-bcv').value      = config.plBCV;

      el('config-loading').classList.add('hidden');
      el('config-form').classList.remove('hidden');
    } catch (e) {
      el('config-loading').classList.add('hidden');
      el('config-error-main').classList.remove('hidden');
      el('config-error-main').innerHTML =
        '⚠️ No se pudo conectar con Odoo.<br>' +
        '<button onclick="GonderApp.saltarConfig()" ' +
        'style="margin-top:12px;padding:10px 20px;background:#F2C200;border:none;border-radius:8px;font-weight:700;cursor:pointer;font-size:15px;">' +
        'Continuar sin listas de precio</button>';
    }
  }

  function guardarConfig() {
    const plE = parseInt(el('sel-estandar').value) || null;
    const plB = parseInt(el('sel-bcv').value) || null;
    config = { plEstandar: plE, plBCV: plB };
    localStorage.setItem(CONFIG_KEY, JSON.stringify(config));
    mostrarApp();
  }

  function saltarConfig() {
    config = { plEstandar: null, plBCV: null };
    localStorage.setItem(CONFIG_KEY, JSON.stringify(config));
    mostrarApp();
  }

  // ── Búsqueda de clientes ──────────────────────────────────────────────────────
  function buscarCliente(q) {
    clearTimeout(clienteTimer);
    const dd = el('dd-cliente');
    if (!q.trim()) {
      dd.classList.remove('open');
      clienteId = null;
      el('inp-cliente-cedula').value = '';
      el('inp-cliente-id').value     = '';
      return;
    }
    clienteTimer = setTimeout(async () => {
      try {
        const d = await fetch(`/api/clientes?q=${encodeURIComponent(q)}`).then(r => r.json());
        if (!d.clientes?.length) {
          dd.innerHTML = '<div class="dd-item text-gray-400 text-xs cursor-default">Sin resultados en Odoo</div>';
          dd.classList.add('open');
          return;
        }
        d.clientes.forEach(c => { _clienteCache[c.id] = c; });
        dd.innerHTML = d.clientes.map(c => `
          <div class="dd-item" onclick="GonderApp.seleccionarCliente(${c.id})">
            <div>
              <div class="text-xs font-semibold text-gray-800">${escHtml(c.name)}</div>
              ${c.vat ? `<div class="text-[10px] text-gray-400">${escHtml(c.vat)}</div>` : ''}
            </div>
          </div>`).join('');
        dd.classList.add('open');
      } catch { dd.classList.remove('open'); }
    }, DEBOUNCE_MS);
  }

  function seleccionarCliente(id) {
    const c = _clienteCache[id];
    if (!c) return;
    clienteId = id;
    el('inp-cliente-nombre').value = c.name;
    el('inp-cliente-cedula').value = c.vat || '';
    el('inp-cliente-id').value     = id;
    el('dd-cliente').classList.remove('open');
  }

  // ── Búsqueda de productos ─────────────────────────────────────────────────────
  function buscarProducto(q) {
    clearTimeout(buscarTimer);
    const dd = el('dd-productos');
    if (!q.trim()) { dd.classList.remove('open'); return; }

    buscarTimer = setTimeout(async () => {
      try {
        const d = await fetch(`/api/productos?q=${encodeURIComponent(q)}${plParams()}`).then(r => r.json());
        if (!d.productos?.length) {
          dd.innerHTML = '<div class="dd-item text-gray-400 text-xs cursor-default">Sin resultados</div>';
          dd.classList.add('open');
          return;
        }
        d.productos.forEach(p => { _prodCache[p.id] = p; });
        dd.innerHTML = d.productos.map(p => `
          <div class="dd-item" onclick="GonderApp.agregarPorId(${p.id})">
            ${p.imagen
              ? `<img src="data:image/png;base64,${p.imagen}" class="prod-img" alt=""/>`
              : '<div class="prod-img-placeholder"><span style="font-size:18px">📦</span></div>'}
            <div class="flex-1 min-w-0">
              <div class="text-xs font-semibold text-gray-800 truncate">${escHtml(p.nombre)}</div>
              <div class="text-[10px] text-gray-400">${escHtml(p.codigo)} · ${escHtml(p.uom)}</div>
              <div class="text-[10px] mt-0.5">
                <span class="text-emerald-500 font-semibold">${fUSD(p.precio_estandar)}</span>
                &nbsp;<span class="text-blue-500 font-semibold">${fUSD(p.precio_bcv)}</span>
              </div>
            </div>
          </div>`).join('');
        dd.classList.add('open');
      } catch { dd.classList.remove('open'); }
    }, DEBOUNCE_MS);
  }

  // Cerrar dropdowns al tocar fuera
  document.addEventListener('click', e => {
    if (!e.target.closest('#inp-buscar') && !e.target.closest('#dd-productos'))
      el('dd-productos').classList.remove('open');
    if (!e.target.closest('#inp-cliente-nombre') && !e.target.closest('#dd-cliente'))
      el('dd-cliente').classList.remove('open');
  });

  // ── Carrito ───────────────────────────────────────────────────────────────────
  function agregarPorId(id) {
    const p = _prodCache[id];
    if (p) _agregarProducto(p);
  }

  function agregarDesdeFicha() {
    if (_fichaActual) {
      _agregarProducto(_fichaActual);
      goTo('scr-main');
    }
  }

  function _agregarProducto(p) {
    const idx = carrito.findIndex(i => i.id === p.id);
    if (idx > -1) {
      carrito[idx].qty = clamp(carrito[idx].qty + 1);
    } else {
      carrito.push({
        id:     p.id,
        nombre: p.nombre,
        codigo: p.codigo || '',
        uom:    p.uom    || '',
        imagen: p.imagen || '',
        qty:    1,
        pe:     p.precio_estandar,
        pb:     p.precio_bcv,
        tasa:   p.tasa_bcv || tasaBCV,
      });
    }
    el('inp-buscar').value = '';
    el('dd-productos').classList.remove('open');
    renderCarrito();
    toast('✓ ' + p.nombre + ' agregado', '#10B981');
  }

  function cambiarCantidad(id, delta) {
    const item = carrito.find(i => i.id === id);
    if (!item) return;
    item.qty = clamp(item.qty + delta);
    const inp = el('qty-' + id);
    if (inp) inp.value = item.qty % 1 === 0 ? item.qty : item.qty.toFixed(2);
    actualizarTotales();
  }

  function setQty(id, val) {
    const item = carrito.find(i => i.id === id);
    if (!item) return;
    const n = parseFloat(val);
    if (isNaN(n) || n <= 0) return;
    item.qty = clamp(n);
    actualizarTotales();
  }

  function eliminarDelCarrito(id) {
    carrito = carrito.filter(i => i.id !== id);
    renderCarrito();
  }

  function nuevoPedido() {
    carrito    = [];
    clienteId  = null;
    ['inp-vendedor','inp-cliente-nombre','inp-cliente-cedula','inp-cliente-id','inp-buscar']
      .forEach(id => { el(id).value = ''; });
    el('dd-productos').classList.remove('open');
    renderCarrito();
    toast('🔄 Pedido nuevo iniciado');
  }

  function renderCarrito() {
    const tbody = el('tbody-carrito');
    if (!carrito.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="text-center py-8 text-gray-400 text-xs">
        Agrega productos desde el buscador o el catálogo</td></tr>`;
      el('tot-usd').textContent = '$ 0.00';
      el('tot-bcv').textContent = '$ 0.00';
      el('tot-bs').textContent  = fBs(0);
      return;
    }

    let totE = 0, totB = 0;
    tbody.innerHTML = carrito.map(item => {
      const subE = item.pe * item.qty;
      const subB = item.pb * item.qty;
      const tasa = item.tasa || tasaBCV;
      totE += subE; totB += subB;
      const qDisp = item.qty % 1 === 0 ? item.qty : item.qty.toFixed(2);

      return `<tr>
        <td class="text-[10px] font-semibold text-gray-400 whitespace-nowrap">${escHtml(item.codigo)}</td>
        <td class="c">
          ${item.imagen
            ? `<img src="data:image/png;base64,${item.imagen}" class="prod-img mx-auto" alt=""/>`
            : '<div class="prod-img-placeholder mx-auto"><span style="font-size:16px">📦</span></div>'}
        </td>
        <td>
          <div class="text-xs font-semibold text-gray-800 leading-tight">${escHtml(item.nombre)}</div>
          <div class="text-[9px] text-gray-400">${escHtml(item.uom)}</div>
        </td>
        <td class="c">
          <div style="display:flex;align-items:center;gap:3px;justify-content:center">
            <button onclick="GonderApp.cambiarCantidad(${item.id},-1)"
              style="width:20px;height:28px;border-radius:5px;border:1px solid #e2e8f0;background:#f8fafc;font-size:14px;font-weight:700;cursor:pointer;display:flex;align-items:center;justify-content:center">−</button>
            <input id="qty-${item.id}" class="qty-inp" type="number"
              min="0.01" step="0.01" value="${qDisp}" inputmode="decimal"
              onchange="GonderApp.setQty(${item.id},this.value)"
              oninput="GonderApp.setQty(${item.id},this.value)"/>
            <button onclick="GonderApp.cambiarCantidad(${item.id},1)"
              style="width:20px;height:28px;border-radius:5px;border:1px solid #e2e8f0;background:#f8fafc;font-size:14px;font-weight:700;cursor:pointer;display:flex;align-items:center;justify-content:center">+</button>
          </div>
        </td>
        <td class="r">
          <div id="sube-${item.id}" class="text-emerald-500 font-bold text-xs whitespace-nowrap">${fUSD(subE)}</div>
        </td>
        <td class="r">
          <div id="subb-${item.id}" class="text-blue-500 font-bold text-xs whitespace-nowrap">${fUSD(subB)}</div>
          <div id="subbs-${item.id}" class="text-[9px] text-blue-700 whitespace-nowrap">${fBs(subB * tasa)}</div>
        </td>
        <td>
          <button onclick="GonderApp.eliminarDelCarrito(${item.id})" class="text-gray-300 hover:text-red-500 p-1">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
              stroke-width="2" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12"/></svg>
          </button>
        </td>
      </tr>`;
    }).join('');

    el('tot-usd').textContent = fUSD(totE);
    el('tot-bcv').textContent = fUSD(totB);
    el('tot-bs').textContent  = fBs(totB * tasaBCV);
  }

  function actualizarTotales() {
    let totE = 0, totB = 0;
    carrito.forEach(item => {
      const subE = item.pe * item.qty;
      const subB = item.pb * item.qty;
      const tasa = item.tasa || tasaBCV;
      totE += subE; totB += subB;
      const se  = el('sube-'  + item.id);
      const sb  = el('subb-'  + item.id);
      const sbs = el('subbs-' + item.id);
      if (se)  se.textContent  = fUSD(subE);
      if (sb)  sb.textContent  = fUSD(subB);
      if (sbs) sbs.textContent = fBs(subB * tasa);
    });
    el('tot-usd').textContent = fUSD(totE);
    el('tot-bcv').textContent = fUSD(totB);
    el('tot-bs').textContent  = fBs(totB * tasaBCV);
  }

  // ── Enviar a Odoo ─────────────────────────────────────────────────────────────
  async function confirmarPedido() {
    if (!carrito.length) { toast('⚠️ El carrito está vacío'); return; }

    const vendedor = el('inp-vendedor').value.trim();
    const cliId    = parseInt(el('inp-cliente-id').value) || null;

    if (!cliId) {
      toast('⚠️ Selecciona un cliente de Odoo', '#ef4444');
      el('inp-cliente-nombre').focus();
      return;
    }

    try {
      toast('⏳ Enviando a Odoo…');
      const r = await fetch('/api/orden', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          vendedor,
          cliente_id: cliId,
          pl_bcv:     config.plBCV,
          items: carrito.map(i => ({
            product_id: i.id,
            qty:        i.qty,
            precio_bcv: i.pb,
          })),
        }),
      });
      const d = await r.json();
      if (d.ok) {
        toast('✅ ' + d.mensaje, '#10B981');
        setTimeout(nuevoPedido, 2000);
      } else {
        toast('❌ ' + d.error, '#ef4444');
      }
    } catch {
      toast('❌ Sin conexión con el servidor', '#ef4444');
    }
  }

  // ── WhatsApp ──────────────────────────────────────────────────────────────────
  function enviarWhatsApp() {
    if (!carrito.length) { toast('⚠️ El carrito está vacío'); return; }

    const vendedor = el('inp-vendedor').value.trim()      || '—';
    const cliente  = el('inp-cliente-nombre').value.trim() || '—';
    const cedula   = el('inp-cliente-cedula').value.trim();
    const hoy      = new Date().toLocaleDateString('es-VE');

    let totE = 0, totB = 0;
    const lineas = carrito.map(i => {
      const subE = i.pe * i.qty, subB = i.pb * i.qty;
      totE += subE; totB += subB;
      const qd = i.qty % 1 === 0 ? i.qty : i.qty.toFixed(2);
      return `• ${i.nombre} (x${qd})\n  USD: ${fUSD(subE)} | BCV: ${fUSD(subB)} (${fBs(subB * (i.tasa || tasaBCV))})`;
    }).join('\n');

    const sep = '─'.repeat(28);
    const msg = [
      `🧾 *COTIZACIÓN GONDER*`,
      `📅 ${hoy}`,
      ``,
      `👤 *Vendedor:* ${vendedor}`,
      `🏪 *Cliente:* ${cliente}${cedula ? ' | ' + cedula : ''}`,
      ``,
      sep, `📦 *PRODUCTOS*`, sep,
      lineas, sep,
      `💵 *Total USD:* ${fUSD(totE)}`,
      `💙 *Total BCV:* ${fUSD(totB)}`,
      `🇻🇪 *Total Bs.:* ${fBs(totB * tasaBCV)}`,
      sep,
      `_Cotización generada por Sistema GONDER_`,
    ].join('\n');

    window.open('https://wa.me/?text=' + encodeURIComponent(msg), '_blank');
    toast('📲 Abriendo WhatsApp…', '#25D366');
  }

  // ── Catálogo ──────────────────────────────────────────────────────────────────
  async function cargarCatalogo() {
    el('cat-list').innerHTML = `
      <div class="text-center py-10 text-gray-400 text-sm flex flex-col items-center gap-2">
        <div class="spinner"></div>Cargando catálogo…
      </div>`;
    try {
      const d = await fetch(`/api/productos?catalogo=1${plParams()}`).then(r => r.json());
      catProductos = d.productos || [];
      catProductos.forEach(p => { _prodCache[p.id] = p; });
      renderCatalogo(catProductos);
    } catch (e) {
      el('cat-list').innerHTML = `
        <div class="text-center py-8 text-red-400 text-xs">⚠️ Error: ${escHtml(e.message)}</div>`;
    }
  }

  function filtrarCatalogo(q) {
    const lq = q.trim().toLowerCase();
    renderCatalogo(lq
      ? catProductos.filter(p =>
          p.nombre.toLowerCase().includes(lq) || p.codigo.toLowerCase().includes(lq))
      : catProductos);
  }

  function renderCatalogo(lista) {
    if (!lista.length) {
      el('cat-list').innerHTML = `<div class="text-center py-8 text-gray-400 text-xs">Sin resultados</div>`;
      return;
    }
    el('cat-list').innerHTML = lista.map(p => `
      <div class="bg-white rounded-xl border border-gray-100 p-3 flex items-center gap-3 cursor-pointer hover:border-yellow-400 transition-colors"
        onclick="GonderApp.verFicha(${p.id})">
        ${p.imagen
          ? `<img src="data:image/png;base64,${p.imagen}" class="prod-img" alt=""/>`
          : '<div class="prod-img-placeholder"><span style="font-size:18px">📦</span></div>'}
        <div class="flex-1 min-w-0">
          <div class="text-xs font-semibold text-gray-800 truncate">${escHtml(p.nombre)}</div>
          <div class="text-[10px] text-gray-400">${escHtml(p.codigo)} · ${escHtml(p.uom)}</div>
          <div class="flex gap-3 mt-0.5">
            <span class="text-emerald-500 font-semibold text-[10px]">${fUSD(p.precio_estandar)}</span>
            <span class="text-blue-500 font-semibold text-[10px]">${fUSD(p.precio_bcv)}</span>
          </div>
        </div>
        <span class="text-gray-300 text-lg">›</span>
      </div>`).join('');
  }

  // ── Ficha de producto ─────────────────────────────────────────────────────────
  async function verFicha(productId) {
    _fichaActual = null;
    goTo('scr-ficha');
    el('ficha-cod').textContent = '';
    el('ficha-body').innerHTML = `
      <div class="text-center py-10 text-gray-400 text-sm flex flex-col items-center gap-2">
        <div class="spinner"></div>Cargando ficha…
      </div>`;

    try {
      const p = await fetch(`/api/producto/${productId}${plParams('?')}`).then(r => r.json());
      if (p.error) throw new Error(p.error);

      _fichaActual = p;
      el('ficha-cod').textContent = p.codigo;

      const bloqueUnidad = `
        <div class="bg-white rounded-xl border border-gray-100 p-3">
          <div class="text-[9px] font-bold text-gray-400 uppercase tracking-widest mb-2">
            📦 Por unidad · ${escHtml(p.uom)}
          </div>
          <div class="flex justify-between items-center mb-1.5">
            <span class="text-[10px] text-gray-500"><span class="badge-e">USD</span> Precio</span>
            <span class="text-emerald-500 font-bold text-sm">${fUSD(p.precio_estandar)}</span>
          </div>
          <div class="flex justify-between items-center mb-1.5">
            <span class="text-[10px] text-gray-500"><span class="badge-b">BCV</span> Precio</span>
            <span class="text-blue-500 font-bold text-sm">${fUSD(p.precio_bcv)}</span>
          </div>
          <div class="flex justify-between items-center">
            <span class="text-[10px] text-gray-500"><span class="badge-b">BCV</span> en Bs.</span>
            <span class="text-blue-700 font-semibold text-xs">${fBs(p.precio_bcv_bs)}</span>
          </div>
        </div>`;

      const bloquesEmb = (p.embalajes || []).map(emb => {
        const peEmb = p.precio_estandar * emb.qty;
        const pbEmb = p.precio_bcv      * emb.qty;
        const bsEmb = p.precio_bcv_bs   * emb.qty;
        return `
          <div class="bg-white rounded-xl border border-gray-100 p-3">
            <div class="text-[9px] font-bold text-gray-400 uppercase tracking-widest mb-1">
              🗃️ Por ${escHtml(emb.nombre)}
            </div>
            <div class="inline-flex items-center bg-yellow-50 border border-yellow-200 rounded px-2 py-0.5 mb-2">
              <span class="text-[9px] font-bold text-yellow-800">x${emb.qty} ${escHtml(p.uom)}</span>
            </div>
            <div class="flex justify-between items-center mb-1.5">
              <span class="text-[10px] text-gray-500"><span class="badge-e">USD</span> Precio ${escHtml(emb.nombre)}</span>
              <span class="text-emerald-500 font-bold text-sm">${fUSD(peEmb)}</span>
            </div>
            <div class="flex justify-between items-center mb-1