export default {
    name: 'RunForm',
    props: {
        methods: Array,
        datasets: Array
    },
    emits: ['submit'],
    data() {
        return {
            form: {
                method: '',
                dataset: '',
                build: true,
                gpu: true
            },
            methodParams: {},
            status: '',
            showLogs: false,
            logsContent: 'Initializing...',
            currentExperiment: null,
            logPollingInterval: null,
            isLogsPaused: false,
            isLive: true,
            collapsed: false
        };
    },
    computed: {
        filteredDatasets() {
            // If no method selected, show all datasets
            if (!this.form.method) {
                return this.datasets;
            }

            // Find the selected method
            const method = this.methods.find(m => m.name === this.form.method);
            if (!method || !method.supported_datasets || method.supported_datasets.length === 0) {
                // No restrictions, show all datasets
                return this.datasets;
            }

            // Filter datasets based on supported_datasets patterns
            return this.datasets.filter(dataset => {
                return method.supported_datasets.some(pattern => {
                    // Convert wildcard pattern to regex
                    // e.g., "bond_*" -> /^bond_.*$/, "*_snapshot" -> /^.*_snapshot$/
                    const regexPattern = pattern
                        .replace(/[.+^${}()|[\]\\]/g, '\\$&') // Escape special regex chars except *
                        .replace(/\*/g, '.*'); // Convert * to .*
                    const regex = new RegExp(`^${regexPattern}$`);
                    return regex.test(dataset.name);
                });
            });
        }
    },
    watch: {
        // Reset dataset when method changes and current dataset is not compatible
        'form.method'() {
            if (this.form.dataset) {
                const isCompatible = this.filteredDatasets.some(d => d.name === this.form.dataset);
                if (!isCompatible) {
                    this.form.dataset = '';
                }
            }
        }
    },
    methods: {
        isNumber(value) {
            return !isNaN(value);
        },

        toggleLogsPause() {
            this.isLogsPaused = !this.isLogsPaused;
            this.isLive = !this.isLogsPaused;
        },

        scrollToBottom() {
            const terminal = document.getElementById('run-terminal');
            if (terminal) {
                terminal.scrollTop = terminal.scrollHeight;
            }
        },

        async loadMethodParams() {
            if (!this.form.method) {
                this.methodParams = {};
                return;
            }

            try {
                const method = this.methods.find(m => m.name === this.form.method);

                if (!method) {
                    const res = await fetch(`/api/methods/${this.form.method}`);
                    const methodData = await res.json();
                    if (methodData.parameters) {
                        const params = {};
                        Object.entries(methodData.parameters)
                            .filter(([key]) => key.startsWith('_'))
                            .forEach(([key, value]) => {
                                params[key] = value;
                            });
                        this.methodParams = params;
                    }
                } else if (method.parameters) {
                    const params = {};
                    Object.entries(method.parameters)
                        .filter(([key]) => key.startsWith('_'))
                        .forEach(([key, value]) => {
                            params[key] = value;
                        });
                    this.methodParams = params;
                } else {
                    this.methodParams = {};
                }
            } catch (error) {
                console.error('Error loading method params:', error);
                this.methodParams = {};
            }
        },

        async submitRun() {
            console.log('[DEBUG] submitRun called');

            this.status = '<div style="color:var(--primary); font-weight:500;">⏳ Starting run...</div>';
            this.showLogs = true;
            this.logsContent = 'Initializing...';
            this.isLogsPaused = false;
            this.isLive = true;

            if (this.logPollingInterval) clearInterval(this.logPollingInterval);

            const params = {};
            Object.entries(this.methodParams).forEach(([key, value]) => {
                // Remove leading underscore from parameter name
                const cleanKey = key.startsWith('_') ? key.substring(1) : key;
                params[cleanKey] = !isNaN(value) ? parseFloat(value) : value;
            });

            console.log('[DEBUG] Making POST request to /api/run');
            try {
                const res = await fetch('/api/run', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        method: this.form.method,
                        dataset: this.form.dataset,
                        build: this.form.build,
                        gpu: this.form.gpu,
                        params
                    })
                });
                console.log('[DEBUG] Response received:', res.status);

                const data = await res.json();
                console.log('[DEBUG] Response data:', data);

                if (res.ok) {
                    this.currentExperiment = data.experiment_name;
                    console.log('[DEBUG] Run started:', this.currentExperiment);
                    this.status = `<div style="background:#D1FAE5; color:#065F46; padding:0.75rem; border-radius:6px; margin-top:1rem;">✅ Run started: <strong>${data.experiment_name}</strong></div>`;
                    this.showLogs = true;
                    this.logsContent = 'Waiting for logs...';

                    // Start polling logs with improved error handling
                    let logLineCount = 0;
                    let consecutiveEmptyChecks = 0;

                    this.logPollingInterval = setInterval(async () => {
                        // Skip polling if paused
                        if (this.isLogsPaused) return;

                        try {
                            console.log('[DEBUG] Polling logs for:', this.currentExperiment);
                            const logRes = await fetch(`/api/experiments/${this.currentExperiment}/logs?tail=1000`);
                            console.log('[DEBUG] Log response status:', logRes.status);

                            if (!logRes.ok) {
                                console.error('[ERROR] Log fetch failed with status:', logRes.status);
                                this.logsContent = `Error fetching logs (HTTP ${logRes.status})`;
                                return;
                            }

                            const logData = await logRes.json();
                            console.log('[DEBUG] Log data received:', logData);

                            if (logData.logs && Array.isArray(logData.logs)) {
                                // Filter out empty lines and join
                                const nonEmptyLogs = logData.logs.filter(line => line && line.trim().length > 0);

                                if (nonEmptyLogs.length > 0) {
                                    console.log('[DEBUG] Retrieved', nonEmptyLogs.length, 'non-empty log lines');

                                    // Check if user is at bottom before updating
                                    const terminal = document.getElementById('run-terminal');
                                    const wasAtBottom = terminal ?
                                        (terminal.scrollHeight - terminal.scrollTop - terminal.clientHeight) < 50 : true;

                                    this.logsContent = nonEmptyLogs.join('\n');
                                    consecutiveEmptyChecks = 0;

                                    // Only auto-scroll if user was at bottom
                                    this.$nextTick(() => {
                                        const terminalEl = document.getElementById('run-terminal');
                                        if (terminalEl && wasAtBottom && !this.isLogsPaused) {
                                            terminalEl.scrollTop = terminalEl.scrollHeight;
                                        }
                                    });

                                    // Check if experiment completed (no new logs)
                                    if (nonEmptyLogs.length === logLineCount) {
                                        consecutiveEmptyChecks++;
                                        if (consecutiveEmptyChecks >= 3) {
                                            const expRes = await fetch(`/api/experiments/${this.currentExperiment}`);
                                            if (expRes.ok) {
                                                const expData = await expRes.json();
                                                console.log('[DEBUG] Experiment status:', expData.status);
                                                if (expData.status !== 'running') {
                                                    clearInterval(this.logPollingInterval);
                                                    this.logPollingInterval = null;
                                                    this.status += '<div style="background:#DBEAFE; color:#1E40AF; padding:0.75rem; border-radius:6px; margin-top:0.5rem;">✨ Experiment completed</div>';
                                                    this.$emit('submit');
                                                }
                                            }
                                        }
                                    }
                                    logLineCount = nonEmptyLogs.length;
                                } else {
                                    console.log('[DEBUG] Logs array contains only empty lines');
                                    if (this.logsContent === 'Waiting for logs...') {
                                        this.logsContent = 'Experiment is running... No output yet.';
                                    }
                                }
                            } else {
                                console.log('[DEBUG] No logs yet, received:', logData);
                                if (this.logsContent === 'Waiting for logs...') {
                                    this.logsContent = 'No logs available yet. Experiment may be initializing...';
                                }
                            }
                        } catch (error) {
                            console.error('[ERROR] Error polling logs:', error);
                            this.logsContent = `Error: ${error.message}`;
                        }
                    }, 2000);

                    // Auto-clear after 10 minutes
                    setTimeout(() => {
                        if (this.logPollingInterval) {
                            clearInterval(this.logPollingInterval);
                            this.logPollingInterval = null;
                        }
                    }, 600000);
                } else {
                    this.status = `<div style="background:#FEE2E2; color:#991B1B; padding:0.75rem; border-radius:6px; margin-top:1rem;">❌ Error: ${data.error}</div>`;
                }
            } catch (error) {
                this.status = `<div style="background:#FEE2E2; color:#991B1B; padding:0.75rem; border-radius:6px; margin-top:1rem;">❌ Error: ${error.message}</div>`;
            }
        }
    },
    template: `
        <div class="section" :class="{ 'section-collapsed': collapsed }">
            <div class="section-header" @click="collapsed = !collapsed" style="cursor:pointer; user-select:none;">
                <h2>🚀 Run</h2>
                <button class="collapse-btn" :title="collapsed ? 'Expand' : 'Collapse'">
                    <svg viewBox="0 0 24 24" style="width:14px;height:14px;stroke:currentColor;stroke-width:2.5;fill:none;transition:transform 0.2s ease;" :style="collapsed ? 'transform:rotate(-90deg)' : ''"><path d="M6 9l6 6 6-6"/></svg>
                </button>
            </div>
            <form v-show="!collapsed" class="form-grid" @submit.prevent="submitRun">
                <div class="grid-2">
                    <div class="param-group">
                        <label>Method</label>
                        <select v-model="form.method" required @change="loadMethodParams">
                            <option value="">Select method...</option>
                            <option v-for="m in methods" :key="m.name" :value="m.name">{{ m.name }}</option>
                        </select>
                    </div>
                    <div class="param-group">
                        <label>Dataset <span v-if="form.method && filteredDatasets.length !== datasets.length" style="font-size:0.75rem; color:var(--text-muted);">({{ filteredDatasets.length }} compatible)</span></label>
                        <select v-model="form.dataset" required>
                            <option value="">Select dataset...</option>
                            <option v-for="d in filteredDatasets" :key="d.name" :value="d.name">{{ d.name }}</option>
                        </select>
                    </div>
                </div>

                <div style="display:flex; gap:1.5rem; align-items:center;">
                    <label style="display:flex; align-items:center; gap:0.3rem; cursor:pointer; font-size:0.75rem;">
                        <input type="checkbox" v-model="form.build"> Build Image
                    </label>
                    <label style="display:flex; align-items:center; gap:0.3rem; cursor:pointer; font-size:0.75rem;">
                        <input type="checkbox" v-model="form.gpu"> Use GPU
                    </label>
                    <button type="submit" class="btn" style="margin-left:auto; display:inline-flex; align-items:center; gap:0.35rem;">
                        <svg viewBox="0 0 24 24" style="width:14px;height:14px;fill:currentColor;stroke:none;"><path d="M8 5v14l11-7z"/></svg> Start
                    </button>
                </div>

                <div v-if="Object.keys(methodParams).length > 0" style="border-top: 1px solid var(--border); padding-top: 0.35rem; margin-top: 0.25rem;">
                    <div style="display:flex; flex-wrap:wrap; gap:0.3rem 0.6rem; align-items:center;">
                        <span style="font-size:0.6rem; text-transform:uppercase; letter-spacing:0.05em; color:var(--text-muted); font-weight:600;">Params</span>
                        <div v-for="(value, key) in methodParams" :key="key" style="display:flex; align-items:center; gap:0.2rem;">
                            <label style="font-size:0.65rem; color:var(--text-muted); white-space:nowrap;">{{ key.substring(1).replace(/_/g, ' ') }}</label>
                            <input :type="isNumber(value) ? 'number' : 'text'"
                                   v-model="methodParams[key]"
                                   step="any"
                                   style="width:5rem; padding:0.15rem 0.3rem; font-size:0.7rem;">
                        </div>
                    </div>
                </div>

                <!-- submit button is inline with checkboxes -->
            </form>

            <div v-show="!collapsed" v-if="status" v-html="status" style="margin-top:0.5rem;"></div>

            <div v-show="!collapsed" v-if="showLogs" style="margin-top: 0.5rem;">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.25rem;">
                    <div style="display:flex; align-items:center; gap:0.5rem;">
                        <span style="font-size:0.7rem; font-family:monospace; color:var(--text-muted);">TERMINAL</span>
                        <span :style="{fontSize:'0.65rem', color: isLive ? 'var(--success)' : '#F59E0B'}">
                            {{ isLive ? '● Live' : '⏸ Paused' }}
                        </span>
                    </div>
                    <div style="display:flex; gap:0.25rem;">
                        <button type="button" class="btn-icon" style="background:#6B7280; color:white; min-width:24px; height:24px;" @click="toggleLogsPause">
                            <svg v-if="isLogsPaused" viewBox="0 0 24 24" style="width:13px;height:13px;fill:currentColor;stroke:none;"><path d="M8 5v14l11-7z"/></svg>
                            <svg v-else viewBox="0 0 24 24" style="width:13px;height:13px;fill:currentColor;stroke:none;"><path d="M6 4h4v16H6zM14 4h4v16h-4z"/></svg>
                        </button>
                        <button type="button" class="btn-icon" style="background:#4B5563; color:white; min-width:24px; height:24px;" @click="scrollToBottom">
                            <svg viewBox="0 0 24 24" style="width:13px;height:13px;"><path d="M12 5v14M5 12l7 7 7-7"/></svg>
                        </button>
                    </div>
                </div>
                <pre id="run-terminal" class="terminal" style="max-height:200px; overflow-y:auto;">{{ logsContent }}</pre>
            </div>
        </div>
    `
};
