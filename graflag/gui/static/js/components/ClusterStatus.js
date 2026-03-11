export default {
    name: 'ClusterStatus',
    props: {
        clusterInfo: {
            type: Object,
            default: null
        }
    },
    computed: {
        isConnected() {
            return this.clusterInfo?.is_connected ?? false;
        },
        isSwarmActive() {
            return this.clusterInfo?.swarm_initialized ?? false;
        },
        managerIp() {
            return this.clusterInfo?.manager_ip ?? '—';
        },
        nodesSummary() {
            if (!this.clusterInfo) return '0/0';
            const nodes = this.clusterInfo.worker_nodes || [];
            const managers = nodes.filter(n => n.is_manager).length;
            const workers = nodes.filter(n => !n.is_manager && n.status?.toLowerCase() === 'ready').length;
            return `${managers}M ${workers}W`;
        },
        hasWorkers() {
            if (!this.clusterInfo) return false;
            const nodes = this.clusterInfo.worker_nodes || [];
            return nodes.filter(n => !n.is_manager && n.status?.toLowerCase() === 'ready').length > 0;
        }
    },
    template: `
        <div class="navbar-cluster" v-if="clusterInfo">
            <div class="cluster-pill">
                <span class="cluster-pill-label">IP</span>
                <span class="cluster-pill-value" style="font-family:'JetBrains Mono',monospace;">{{ managerIp }}</span>
            </div>
            <div class="cluster-pill">
                <span class="cluster-dot" :class="isConnected ? 'dot-green' : 'dot-red'"></span>
                <span class="cluster-pill-value">SSH</span>
            </div>
            <div class="cluster-pill">
                <span class="cluster-dot" :class="isSwarmActive ? 'dot-green' : 'dot-red'"></span>
                <span class="cluster-pill-value">Swarm</span>
            </div>
            <div class="cluster-pill">
                <span class="cluster-dot" :class="hasWorkers ? 'dot-green' : 'dot-amber'"></span>
                <span class="cluster-pill-value">{{ nodesSummary }}</span>
            </div>
        </div>
        <div class="navbar-cluster" v-else>
            <div class="cluster-pill">
                <span class="cluster-dot dot-amber"></span>
                <span class="cluster-pill-value" style="color:var(--text-muted);">Loading...</span>
            </div>
        </div>
    `
};
