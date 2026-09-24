// Vue is loaded as a global by index.html (vue.global.js), the same way
// app.js takes it. An ESM import of Vue here would pull a second copy of the
// runtime and its refs would not be reactive in the app's render tree.
const { ref } = Vue;

/**
 * One fetch, with the three states a panel actually has.
 *
 * Every loader in app.js used to be `try { ... } catch { xs.value = [] }`, and
 * index.html had no error surface at all -- so an unreachable cluster rendered
 * exactly like a healthy empty one, and a dashboard that had lost its
 * connection looked like a cluster that had lost its experiments. That is the
 * worst failure a monitoring UI can have, because it is silent and it is
 * wrong in the reassuring direction.
 *
 * `state` is one of 'idle' | 'loading' | 'ready' | 'error', so a template can
 * tell "nothing yet", "nothing there" and "could not ask" apart.
 *
 * @param {string} url            endpoint to fetch
 * @param {object} options
 * @param {*}      options.initial value before the first successful load
 * @param {(body:any)=>any} options.select  pick the payload out of the body
 */
export function useResource(url, { initial = null, select = (b) => b } = {}) {
    const data = ref(initial);
    const state = ref('idle');
    const error = ref(null);
    const lastLoaded = ref(null);

    /**
     * @param {object} params query parameters
     * @param {boolean} quiet when true the panel keeps showing what it has
     *        instead of flashing a spinner. Used by the 2-second poll: a
     *        refresh that blanks the table every tick is unreadable.
     */
    const load = async (params = {}, { quiet = false } = {}) => {
        if (!quiet) state.value = 'loading';
        try {
            const query = new URLSearchParams(params).toString();
            const res = await fetch(query ? `${url}?${query}` : url);
            if (!res.ok) {
                // A JSON {error: ...} is the server telling us why; prefer it
                // over the bare status code.
                let detail = `HTTP ${res.status}`;
                try {
                    const body = await res.json();
                    if (body && body.error) detail = body.error;
                } catch (_) { /* not JSON; the status is all we have */ }
                throw new Error(detail);
            }
            data.value = select(await res.json());
            state.value = 'ready';
            error.value = null;
            lastLoaded.value = new Date();
            return data.value;
        } catch (err) {
            // Deliberately does NOT clear data: the last good list is more
            // useful than an empty one, and `state` already says it is stale.
            state.value = 'error';
            error.value = err.message || String(err);
            console.error(`Failed to load ${url}:`, err);
            return null;
        }
    };

    return { data, state, error, lastLoaded, load };
}
