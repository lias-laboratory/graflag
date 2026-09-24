import { enableColumnResize } from '../columnResize.js';

export default {
    name: 'DataTable',
    props: {
        title: String,
        icon: String,
        data: {
            type: Array,
            required: true
        },
        columns: {
            type: Array,
            required: true
        },
        keyField: {
            type: String,
            default: 'name'
        },
        page: {
            type: Number,
            default: 1
        },
        itemsPerPage: {
            type: Number,
            default: 5
        },
        // Server-side mode. The component slices `data` itself, which is
        // right while the caller holds the whole list -- but the experiments
        // endpoint returns one page at a time, and slicing a page would show
        // five of five and then nothing. With `serverTotal` set, `data` is
        // taken to be the current page already.
        serverTotal: {
            type: Number,
            default: null
        },
        // Panel state, so 'asking', 'nothing there' and 'could not ask' are
        // three different renderings rather than one blank table.
        state: {
            type: String,
            default: 'ready'        // idle | loading | ready | error
        },
        errorMessage: {
            type: String,
            default: null
        }
    },
    emits: ['update:page'],
    data() {
        return {
            collapsed: false
        };
    },
    mounted() {
        // The handles are attached to the rendered table rather than
        // templated in, so one implementation serves this component and the
        // experiments table, which is still inline markup.
        this.$nextTick(() => {
            const table = this.$el.querySelector('.table-container table');
            this.teardownResize = enableColumnResize(
                table, this.storageKey, this.declaredWidths);
        });
    },
    beforeUnmount() {
        if (this.teardownResize) this.teardownResize();
    },
    updated() {
        // A panel that was empty, collapsed or erroring renders no table at
        // all; when one comes back it is a new element and needs handles.
        const table = this.$el.querySelector('.table-container table');
        if (table && table.dataset.resizable !== '1') {
            this.teardownResize = enableColumnResize(
                table, this.storageKey, this.declaredWidths);
        }
    },
    watch: {
        'data.length'(newLen, oldLen) {
            // In server-side mode a short page is the last page, not an
            // out-of-range one; resetting here would bounce the user to
            // page 1 every time they reached the end.
            if (this.serverSide) return;
            if (this.totalPages > 0 && this.page > this.totalPages) {
                this.$nextTick(() => {
                    this.$emit('update:page', 1);
                });
            }
        }
    },
    computed: {
        storageKey() {
            return this.title.toLowerCase().replace(/\s+/g, '-');
        },
        declaredWidths() {
            return this.columns.map(c => c.width || '');
        },
        serverSide() {
            return this.serverTotal !== null && this.serverTotal !== undefined;
        },
        totalPages() {
            if (this.serverSide) {
                return Math.max(1, Math.ceil(this.serverTotal / this.itemsPerPage));
            }
            if (!this.data || this.data.length === 0) return 1;
            return Math.ceil(this.data.length / this.itemsPerPage);
        },
        startIndex() {
            return (this.page - 1) * this.itemsPerPage;
        },
        endIndex() {
            return this.startIndex + this.itemsPerPage;
        },
        paginatedData() {
            if (!this.data || !Array.isArray(this.data)) return [];
            // Already one page in server-side mode; slicing again would drop
            // everything past the first itemsPerPage of the page.
            if (this.serverSide) return this.data;
            return this.data.slice(this.startIndex, this.endIndex);
        },
        currentPage() {
            return Math.min(this.page, this.totalPages);
        },
        fillerRows() {
            // Empty rows padding a short page out to a full one.
            //
            // Every height-based attempt needed a guess at the row height and
            // every guess was wrong somewhere: reserving 201px for a page
            // that measures 226.7px padded the *short* page and left the full
            // ones to grow. Rows are not even the same height between tables
            // -- Methods renders 32.7px, Datasets 31.7px -- so no single
            // constant works. Padding with real rows sidesteps the
            // arithmetic: whatever a row measures, five of them measure the
            // same on every page.
            //
            // Only where there is something to page through; a single-page
            // table cannot jump and should not carry dead space.
            if (this.totalPages <= 1) return 0;
            return Math.max(0, this.itemsPerPage - this.paginatedData.length);
        }
    },
    methods: {
        prevPage() {
            if (this.page > 1) {
                this.$emit('update:page', this.page - 1);
            }
        },
        nextPage() {
            if (this.page < this.totalPages) {
                this.$emit('update:page', this.page + 1);
            }
        }
    },
    template: `
        <div class="section" :class="{ 'section-collapsed': collapsed }">
            <div class="section-header" @click="collapsed = !collapsed" style="cursor:pointer; user-select:none;">
                <h2>{{ icon }} {{ title }} <span style="color:var(--text-muted); font-weight:400; font-size:0.7rem;">{{ data.length }}</span></h2>
                <button class="collapse-btn" :title="collapsed ? 'Expand' : 'Collapse'">
                    <svg viewBox="0 0 24 24" style="width:14px;height:14px;stroke:currentColor;stroke-width:2.5;fill:none;transition:transform 0.2s ease;" :style="collapsed ? 'transform:rotate(-90deg)' : ''"><path d="M6 9l6 6 6-6"/></svg>
                </button>
            </div>
            <div v-show="!collapsed">
                <div v-if="data.length === 0" style="color:var(--text-muted); padding:0.5rem; text-align:center; font-size:0.8rem;">
                    No {{ title.toLowerCase() }} found
                </div>
                <div v-else class="table-container">
                    <table>
                        <!-- Declared widths with table-layout: fixed.
                             Content-sized columns re-measured on every page,
                             so text slid sideways as you paged: measured
                             shifts of 50px in Methods, 53px in Datasets and
                             93px in Experiments. A column's width should be
                             a property of the column, not of whichever five
                             rows happen to be on screen. Columns without an
                             explicit width share what is left. -->
                        <!-- No :style here on purpose. Vue re-applies a
                             bound style on re-render, which overwrote every
                             width the user had dragged; enableColumnResize
                             sets them from the same column definitions. -->
                        <colgroup>
                            <col v-for="col in columns" :key="'col-' + col.key">
                        </colgroup>
                        <thead>
                            <tr>
                                <th v-for="col in columns" :key="col.key">{{ col.label }}</th>
                            </tr>
                        </thead>
                        <tbody>
                            <!-- Three states, three renderings. Previously a
                                 failed fetch, a pending one and a genuinely
                                 empty result all produced the same blank
                                 table body. -->
                            <tr v-if="state === 'error'">
                                <td :colspan="columns.length" class="panel-state panel-state--error">
                                    Could not load. {{ errorMessage || 'The cluster did not answer.' }}
                                </td>
                            </tr>
                            <tr v-else-if="state === 'loading' && paginatedData.length === 0">
                                <td :colspan="columns.length" class="panel-state">Loading…</td>
                            </tr>
                            <tr v-else-if="paginatedData.length === 0">
                                <td :colspan="columns.length" class="panel-state panel-state--empty">
                                    Nothing here yet.
                                </td>
                            </tr>
                            <tr v-for="item in paginatedData" :key="item[keyField]">
                                <td v-for="col in columns" :key="col.key">
                                    <slot :name="'cell-' + col.key" :item="item">
                                        {{ col.format ? col.format(item[col.key]) : item[col.key] }}
                                    </slot>
                                </td>
                            </tr>
                            <tr v-for="n in fillerRows" :key="'filler-' + n" class="filler-row" aria-hidden="true">
                                <!-- An invisible tag, not a bare space: a row
                                     carrying a .tag is taller than one
                                     carrying text, so a blank filler left the
                                     last page 2.8px short. Matching the
                                     structure matches the height without
                                     hard-coding one. -->
                                <td :colspan="columns.length"><span class="tag" style="visibility:hidden;">&nbsp;</span></td>
                            </tr>
                        </tbody>
                    </table>
                    <div v-if="totalPages > 1" style="display:flex; justify-content:space-between; align-items:center; padding:0.3rem 0.5rem; border-top:1px solid var(--border);">
                        <div style="color:var(--text-muted); font-size:0.7rem;">
                            {{ startIndex + 1 }}-{{ Math.min(endIndex, data.length) }} of {{ data.length }}
                        </div>
                        <div style="display:flex; gap:0.3rem; align-items:center;">
                            <button class="btn-page" :disabled="page === 1"
                                    @click.stop="prevPage">
                                <svg viewBox="0 0 24 24"><path d="M15 18l-6-6 6-6"/></svg>
                            </button>
                            <span style="font-size:0.7rem; color:var(--text-muted); padding:0 0.25rem;">
                                {{ page }}/{{ totalPages }}
                            </span>
                            <button class="btn-page" :disabled="page === totalPages"
                                    @click.stop="nextPage">
                                <svg viewBox="0 0 24 24"><path d="M9 18l6-6-6-6"/></svg>
                            </button>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    `
};
