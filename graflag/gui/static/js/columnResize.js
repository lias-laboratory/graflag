/**
 * Draggable column borders for the dashboard's tables.
 *
 * The tables declare their widths (`table-layout: fixed` plus a colgroup)
 * because content-sized columns moved by up to 93px as you paged through
 * them. Declared widths fix that but make one set of proportions everyone's
 * proportions, and the right split depends on what you are reading -- a long
 * description, or the full name of an experiment. This gives the borders a
 * grip without giving up the stability.
 *
 * Widths are kept as percentages, not pixels: a layout saved at one window
 * size has to survive being reopened at another. Dragging moves the border
 * between two neighbours and leaves their sum alone, so the table never
 * overflows its panel and the columns beyond the one you grabbed stay put.
 */

const MIN_PX = 48;          // a column narrower than this cannot be grabbed back
const STORE_PREFIX = 'graflag.columns.';

const readSaved = (key) => {
    try {
        const raw = localStorage.getItem(STORE_PREFIX + key);
        if (!raw) return null;
        const saved = JSON.parse(raw);
        return Array.isArray(saved) && saved.every(n => typeof n === 'number')
            ? saved : null;
    } catch (e) {
        return null;            // private window, blocked storage, bad JSON
    }
};

const writeSaved = (key, pct) => {
    try {
        localStorage.setItem(STORE_PREFIX + key, JSON.stringify(pct));
    } catch (e) {
        /* storage full or blocked; resizing still works for this session */
    }
};

export function clearSavedLayout(key) {
    try {
        localStorage.removeItem(STORE_PREFIX + key);
    } catch (e) { /* nothing to do */ }
}

/**
 * @param {HTMLTableElement} table   must already contain a <colgroup>
 * @param {string} storageKey        distinguishes one table's layout
 * @param {string[]} [declaredWidths] CSS widths, one per column, when the
 *        caller owns them. Pass these rather than binding them in a
 *        framework template: a re-render re-applies the bound value over
 *        whatever the user dragged, which is what silently discarded every
 *        saved layout on the first attempt at this.
 * @returns {function} removes the handles and listeners
 */
export function enableColumnResize(table, storageKey, declaredWidths) {
    if (!table || table.dataset.resizable === '1') return () => {};
    const cols = [...table.querySelectorAll('colgroup col')];
    const ths = [...table.querySelectorAll('thead th')];
    if (cols.length < 2 || cols.length !== ths.length) return () => {};
    table.dataset.resizable = '1';

    const total = () => table.getBoundingClientRect().width || 1;
    const rendered = () => {
        const t = total();
        return ths.map(th => th.getBoundingClientRect().width / t * 100);
    };
    const apply = (pct) => pct.forEach((p, i) => { cols[i].style.width = p + '%'; });

    if (Array.isArray(declaredWidths) && declaredWidths.length === cols.length) {
        declaredWidths.forEach((w, i) => { if (w) cols[i].style.width = w; });
    }
    // Measured after the declared widths are in place, so "reset" goes back
    // to the layout the table was designed with rather than to whatever the
    // browser happened to compute first.
    const defaults = rendered();

    const saved = readSaved(storageKey);
    if (saved && saved.length === cols.length) apply(saved);

    let drag = null;

    const onMove = (evt) => {
        if (!drag) return;
        const span = total();
        const minPct = MIN_PX / span * 100;
        let delta = (evt.clientX - drag.startX) / span * 100;
        // Clamp so neither neighbour is squeezed below the minimum. Without
        // this a fast drag collapses a column to zero and there is no border
        // left to grab.
        delta = Math.max(minPct - drag.a, Math.min(delta, drag.b - minPct));
        const next = drag.base.slice();
        next[drag.i] = drag.a + delta;
        next[drag.i + 1] = drag.b - delta;
        apply(next);
        drag.result = next;
    };

    const onUp = () => {
        if (!drag) return;
        if (drag.result) writeSaved(storageKey, drag.result);
        drag = null;
        window.removeEventListener('mousemove', onMove);
        window.removeEventListener('mouseup', onUp);
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
    };

    const onDown = (evt, i) => {
        evt.preventDefault();
        evt.stopPropagation();      // the section header toggles on click
        const base = rendered();
        drag = { i, startX: evt.clientX, base, a: base[i], b: base[i + 1],
                 result: null };
        window.addEventListener('mousemove', onMove);
        window.addEventListener('mouseup', onUp);
        document.body.style.cursor = 'col-resize';
        document.body.style.userSelect = 'none';
    };

    const onReset = (evt) => {
        evt.preventDefault();
        evt.stopPropagation();
        apply(defaults);
        clearSavedLayout(storageKey);
    };

    const handles = [];
    ths.forEach((th, i) => {
        if (i === ths.length - 1) return;   // nothing to its right to trade with
        th.classList.add('th-resizable');
        const grip = document.createElement('span');
        grip.className = 'col-resizer';
        grip.title = 'Drag to resize. Double-click to reset.';
        grip.addEventListener('mousedown', (e) => onDown(e, i));
        grip.addEventListener('dblclick', onReset);
        grip.addEventListener('click', (e) => e.stopPropagation());
        th.appendChild(grip);
        handles.push(grip);
    });

    return () => {
        onUp();
        handles.forEach(h => h.remove());
        delete table.dataset.resizable;
    };
}
