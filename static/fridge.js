(() => {
    const $ = id => document.getElementById(id);
    const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    let S = null, toastTimer;

    async function api(url, method = 'GET', body) {
        const r = await fetch(url, {
            method,
            headers: { 'Content-Type': 'application/json' },
            body: body ? JSON.stringify(body) : undefined
        });
        let data = {};
        try { data = await r.json(); } catch (e) {}
        if (!r.ok) throw new Error(data.error || 'Xəta baş verdi');
        return data;
    }

    function toast(msg, bad) {
        const t = $('toast');
        t.textContent = msg;
        t.className = 'sf-toast show' + (bad ? ' bad' : '');
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => { t.className = 'sf-toast'; }, 3000);
    }

    async function act(url, method, body, okMsg) {
        try {
            const r = await api(url, method, body);
            if (okMsg) toast(okMsg);
            await refresh();
            return r;
        } catch (e) { toast(e.message, true); }
    }

    /* ---------- Tablar ---------- */
    function showTab(name) {
        document.querySelectorAll('#sf-tabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === name));
        document.querySelectorAll('[data-pane]').forEach(p => p.classList.toggle('on', p.dataset.pane === name));
    }
    document.querySelectorAll('#sf-tabs button').forEach(b => b.onclick = () => showTab(b.dataset.tab));

    /* ---------- Render ---------- */
    const STATUS_TEXT = { ok: 'Normal', warn: 'Diqqət', alert: 'Problem' };

    function renderHeader() {
        document.querySelectorAll('#sf-mode button').forEach(b => b.classList.toggle('on', b.dataset.mode === S.mode));
        const bad = S.zones.filter(z => z.status !== 'ok').length;
        const high = S.suggestions.filter(s => s.severity === 'high').length;
        const total = bad + high;
        const level = S.zones.some(z => z.status === 'alert') ? 'alert' : (total ? 'warn' : 'ok');
        const el = $('sf-status');
        el.className = 'sf-status ' + level;
        el.querySelector('span').textContent = total ? `${total} məqam diqqət tələb edir` : 'Hər şey normaldır';
    }

    function renderZones() {
        const manual = S.mode === 'manual';
        $('mode-note').textContent = manual
            ? 'Manual rejim: hədəf temperaturu − və + düymələri ilə dəyişin. AI yalnız xəbərdarlıq edir.'
            : 'AI temperaturu avtomatik idarə edir.';
        $('zone-grid').innerHTML = S.zones.map(z => `
            <div class="zone-card ${z.status}">
                <div class="zc-top"><span>${esc(z.name)}</span><em>${STATUS_TEXT[z.status]}</em></div>
                <div class="zc-temp">${z.temp.toFixed(1)}<small>°C</small></div>
                ${manual ? `
                <div class="zc-step">
                    <button type="button" data-sp="${esc(z.key)}" data-d="-0.5" aria-label="Azalt">−</button>
                    <span>hədəf ${z.setpoint.toFixed(1)}°C</span>
                    <button type="button" data-sp="${esc(z.key)}" data-d="0.5" aria-label="Artır">+</button>
                </div>` : `<div class="zc-sub">hədəf ${z.setpoint.toFixed(1)}°C</div>`}
                <div class="zc-row">
                    <span>💧 ${z.humidity.toFixed(0)}%</span>
                    <span>📦 ${z.items}</span>
                    ${z.gas >= 2 ? `<span>🌫 ${z.gas.toFixed(1)}</span>` : ''}
                </div>
            </div>`).join('');
    }

    function dayBadge(p) {
        if (p.days_left === null) return '';
        if (p.days_left < 0) return '<em class="bad">vaxtı keçib</em>';
        if (p.days_left <= 2) return `<em class="warn">${p.days_left === 0 ? 'bu gün' : p.days_left + ' gün'}</em>`;
        return `<em>${p.days_left} gün</em>`;
    }

    function renderProducts() {
        const mis = new Set(S.suggestions.filter(s => s.type === 'move').map(s => s.product_id));
        if (!S.products.length) {
            $('p-list').innerHTML = '<p class="empty">Soyuducu boşdur. Məhsul əlavə et və ya demo məlumat yüklə.</p>';
            return;
        }
        $('p-list').innerHTML = S.zones.map(z => {
            const items = S.products.filter(p => p.zone_key === z.key);
            if (!items.length) return '';
            return `<div class="pz"><h5>${esc(z.name)} <span>${items.length}</span></h5><div class="pchips">` +
                items.map(p => `<span class="pchip ${mis.has(p.id) ? 'mis' : ''}" title="${esc(p.category_label)}">
                    ${mis.has(p.id) ? '⚠ ' : ''}${esc(p.name)} ${dayBadge(p)}
                    <button type="button" data-del="${p.id}" aria-label="Sil">×</button></span>`).join('') +
                '</div></div>';
        }).join('');
    }

    function sgAction(s) {
        if (s.type === 'move') return `<button type="button" data-move="${s.product_id}" data-zone="${esc(s.to_zone)}">Köçür</button>`;
        if (s.action === 'discard') return `<button type="button" data-del="${s.product_id}">Sil</button>`;
        if (s.action === 'recipe') return '<a href="/scanner">Resept tap →</a>';
        return '';
    }

    function renderSuggestions() {
        const n = S.suggestions.length;
        $('ai-badge').hidden = !n;
        $('ai-badge').textContent = n;
        if (!n) {
            $('sg-list').innerHTML = '<div class="allok">✓ Hər şey qaydasındadır. Heç bir təklif yoxdur.</div>';
            return;
        }
        const moves = S.suggestions.filter(s => s.type === 'move');
        let html = moves.length > 1
            ? `<button type="button" class="apply-all" data-applyall="1">Bütün köçürmələri təsdiqlə (${moves.length})</button>` : '';
        html += S.suggestions.map(s => `
            <div class="sg ${s.severity}">
                <div class="sg-body"><strong>${esc(s.title)}</strong><p>${esc(s.text)}</p></div>
                <div class="sg-act">${sgAction(s)}</div>
            </div>`).join('');
        $('sg-list').innerHTML = html;
    }
    const SHOP_LABEL = { buy: 'Almaq lazımdır', soon: 'Tezliklə', skip: 'Almayın', ok: 'Kifayətdir' };

    function renderShop() {
        const items = S.shopping.items;
        const count = st => items.filter(i => i.status === st).length;
        const nBuy = count('buy');
        $('shop-badge').hidden = !nBuy;
        $('shop-badge').textContent = nBuy;

        $('shop-sum').innerHTML =
            `<div class="buy"><b>${nBuy}</b><span>almaq lazımdır</span></div>` +
            `<div class="soon"><b>${count('soon')}</b><span>bu həftə</span></div>` +
            `<div class="skip"><b>${count('skip')}</b><span>almayın (artıq var)</span></div>`;

        if (!items.length) {
            $('shop-list').innerHTML = '<p class="empty">İstifadə statistikası yoxdur. Aşağıdakı «Nümunə statistika yüklə» düyməsini bas.</p>';
            return;
        }
        $('shop-list').innerHTML = items.map(i => {
            const max = Math.max(...i.weeks, 1);
            const bars = i.weeks.slice().reverse()
                .map(w => `<i style="height:${Math.max(8, (w / max) * 100)}%" title="${w} ədəd"></i>`).join('');
            return `<div class="shop-item ${i.status}">
                <div class="shop-bars" title="Son 4 həftənin istifadəsi">${bars}</div>
                <div class="shop-main">
                    <strong>${esc(i.name)}</strong>
                    <p>${esc(i.text)}</p>
                    <small>Stok: ${i.stock} · həftədə ~${i.weekly} ədəd</small>
                </div>
                <div class="shop-side"><em>${SHOP_LABEL[i.status]}</em>${i.buy ? `<b>+${i.buy}</b>` : ''}</div>
            </div>`;
        }).join('');
    }
    function renderLog() {
        $('ev-list').innerHTML = S.events.length
            ? S.events.map(e => `<div class="ev ${esc(e.kind)}"><time>${esc(e.ts.slice(11, 19))}</time><span>${esc(e.message)}</span></div>`).join('')
            : '<p class="empty">Hələ hadisə yoxdur.</p>';
    }

    async function refresh() {
        try {
            S = await api('/api/fridge/state');
            renderHeader(); renderZones(); renderProducts(); renderSuggestions(); renderShop(); renderLog();        } catch (e) {
            $('sf-status').className = 'sf-status off';
            $('sf-status').querySelector('span').textContent = 'Serverlə əlaqə yoxdur';
        }
    }

    /* ---------- Hadisələr ---------- */
    $('sf-mode').onclick = e => {
        const b = e.target.closest('[data-mode]');
        if (b && S && b.dataset.mode !== S.mode)
            act('/api/fridge/mode', 'POST', { mode: b.dataset.mode },
                b.dataset.mode === 'auto' ? 'AI idarəetməni geri aldı' : 'Manual rejim aktivdir');
    };

    $('zone-grid').onclick = e => {
        const b = e.target.closest('[data-sp]');
        if (!b || !S) return;
        const z = S.zones.find(x => x.key === b.dataset.sp);
        if (z) act(`/api/fridge/zone/${z.key}/setpoint`, 'POST', { value: z.setpoint + parseFloat(b.dataset.d) });
    };

    // Qapı açılması və bağlanması simulyasiyası
    let doorIsOpen = false;
    const btnDoor = $('btn-door');
    if (btnDoor) {
        btnDoor.onclick = async () => {
            doorIsOpen = !doorIsOpen;
            const r = await act('/api/fridge/door-open', 'POST', { isOpen: doorIsOpen }, 
                doorIsOpen ? 'Qapı açıldı: temperatur qalxır' : 'Qapı bağlandı: soyuducu soyuyur');
            if (r) {
                btnDoor.textContent = doorIsOpen ? 'Qapını bağla (testi dayandır)' : 'Qapı açılması testi (simulyasiya)';
                btnDoor.style.background = doorIsOpen ? '#d97706' : '';
                btnDoor.style.color = doorIsOpen ? '#fff' : '';
            }
        };
    }

    $('btn-seed').onclick = () => act('/api/fridge/seed', 'POST', null, 'Demo məlumat yükləndi');
    $('btn-clear').onclick = () => { if (confirm('Bütün məhsullar silinsin?')) act('/api/fridge/clear', 'POST', null, 'Təmizləndi'); };

    $('add-form').onsubmit = async e => {
        e.preventDefault();
        try {
            const r = await api('/api/fridge/product', 'POST', { name: $('p-name').value, expiry: $('p-exp').value });
            toast(`«${r.category}» kimi təsnif edildi → ${r.zone_name}`);
            e.target.reset();
            await refresh();
        } catch (err) { toast(err.message, true); }
    };

    const listClick = e => {
        const del = e.target.closest('[data-del]');
        const mv = e.target.closest('[data-move]');
        const all = e.target.closest('[data-applyall]');
        if (del) act(`/api/fridge/product/${del.dataset.del}`, 'DELETE', null, 'Məhsul silindi');
        else if (mv) act(`/api/fridge/product/${mv.dataset.move}/move`, 'POST', { zone: mv.dataset.zone }, 'Məhsul köçürüldü');
        else if (all) act('/api/fridge/apply-all', 'POST', null, 'Bütün təkliflər tətbiq olundu');
    };
    $('p-list').onclick = listClick;
    $('sg-list').onclick = listClick;

    /* ---------- Alış ---------- */
    $('btn-usage-seed').onclick = () => act('/api/fridge/usage/seed', 'POST', null, 'Nümunə statistika yükləndi');
    $('btn-usage-clear').onclick = () => { if (confirm('Bütün istifadə statistikası silinsin?')) act('/api/fridge/usage/clear', 'POST', null, 'Statistika təmizləndi'); };

    $('use-form').onsubmit = async e => {
        e.preventDefault();
        const r = await act('/api/fridge/usage', 'POST', { name: $('u-name').value, qty: $('u-qty').value }, 'İstifadə qeyd edildi');
        if (r) e.target.reset(), $('u-qty').value = 1;
    };

    $('btn-shop-ai').onclick = async () => {
        const out = $('shop-ai-out');
        out.hidden = false;
        out.textContent = 'AI təhlil edir…';
        try { out.textContent = (await api('/api/fridge/shopping/ai', 'POST')).summary; }
        catch (e) { out.textContent = e.message; }
    };

    $('btn-shop-copy').onclick = async () => {
        const list = S.shopping.items.filter(i => i.buy > 0).map(i => `- ${i.name} × ${i.buy}`).join('\n');
        if (!list) return toast('Alınacaq məhsul yoxdur');
        try { await navigator.clipboard.writeText(list); toast('Siyahı kopyalandı'); }
        catch (e) { toast('Kopyalamaq alınmadı', true); }
    };
    /* ---------- Saat və dövri yeniləmə ---------- */
    const tickClock = () => { $('sf-time').textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); };
    tickClock();
    setInterval(tickClock, 1000);

    refresh();
    setInterval(refresh, 3000);
})();