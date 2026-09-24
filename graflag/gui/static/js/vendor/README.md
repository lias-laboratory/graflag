# Vendored front-end libraries

`index.html` used to load Vue and Socket.IO from unpkg.com and cdn.socket.io.
Measured here: one page load in three came back with `typeof Vue === "undefined"`,
and the dashboard then rendered its own template source -- raw `{{ }}` mustaches,
every panel gone, no error anywhere. A cluster dashboard also has no business
requiring internet access: the deployment assumption is a private network with
a Swarm manager on it, which may have no route off-site at all.

| File | Version | Source |
|---|---|---|
| `vue.global.prod.js` | 3.5.13 | `https://unpkg.com/vue@3.5.13/dist/vue.global.prod.js` |
| `socket.io.min.js` | 4.5.4 | `https://cdn.socket.io/4.5.4/socket.io.min.js` |

Both are MIT licensed; the headers are intact at the top of each file.

`vue.global.prod.js` is the *full* build, not `vue.runtime.*`: the dashboard
declares its templates in the page and in each component's `template:` string,
so it needs the runtime compiler. Swapping in a runtime-only build renders
nothing.

To update, re-download the exact URL above and bump the version here.
