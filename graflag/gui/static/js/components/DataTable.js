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
        }
    },
    emits: ['update:page'],
    data() {
        return {
            collapsed: false
        };
    },
    watch: {
        'data.length'(newLen, oldLen) {
            if (this.totalPages > 0 && this.page > this.totalPages) {
                this.$nextTick(() => {
                    this.$emit('update:page', 1);
                });
            }
        }
    },
    computed: {
        totalPages() {
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
            return this.data.slice(this.startIndex, this.endIndex);
        },
        currentPage() {
            return Math.min(this.page, this.totalPages);
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
                        <thead>
                            <tr>
                                <th v-for="col in columns" :key="col.key">{{ col.label }}</th>
                            </tr>
                        </thead>
                        <tbody>
                            <tr v-for="item in paginatedData" :key="item[keyField]">
                                <td v-for="col in columns" :key="col.key">
                                    <slot :name="'cell-' + col.key" :item="item">
                                        {{ col.format ? col.format(item[col.key]) : item[col.key] }}
                                    </slot>
                                </td>
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
