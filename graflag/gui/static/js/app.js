import { enableColumnResize } from './columnResize.js';
import ClusterStatus from './components/ClusterStatus.js';
import RunForm from './components/RunForm.js';
import DataTable from './components/DataTable.js';
import ExperimentModal from './components/ExperimentModal.js';
import { useResource } from './composables/useResource.js';

const { createApp, ref, computed, onMounted, nextTick } = Vue;

createApp({
    components: {
        ClusterStatus,
        RunForm,
        DataTable,
        ExperimentModal
    },
    setup() {
        // State
        const isLoading = ref(true);
        const clusterInfo = ref(null);
        const methods = ref([]);
        const datasets = ref([]);
        const experiments = ref([]);
        // Total on the share and whether the list above is only part of it.
        const experimentTotal = ref(null);
        const experimentsTruncated = ref(false);
        // Server-side paging. The page used to be a client slice of whatever
        // the API had capped, so the last page was the end of the cap rather
        // than the end of the data.
        const experimentPageSize = 5;
        const experimentLimit = ref(experimentPageSize);
        const services = ref([]);
        
        // Modal (kept for backward compat)
        const showModal = ref(false);
        const modalContent = ref('');

        // Inline detail panel
        const viewMode = ref(null); // 'logs', 'eval', null
        const viewExperiment = ref(null); // experiment name
        const viewLogs_text = ref('');
        const viewEval_data = ref(null);
        const viewResults_data = ref(null);
        const viewLogsPaused = ref(false);
        
        // Pagination
        const experimentPage = ref(1);
        const methodPage = ref(1);
        const datasetPage = ref(1);
        const servicePage = ref(1);

        // Experiment pagination (inline, not via DataTable)
        const experimentTotalPages = computed(() => {
            // Off the server's total, so the page count is the real one.
            const total = experimentTotal.value;
            if (total === null || total === undefined) {
                return Math.max(1, Math.ceil(
                    (experiments.value?.length || 0) / experimentPageSize));
            }
            return Math.max(1, Math.ceil(total / experimentPageSize));
        });
        // The server already returned exactly this page, so there is nothing
        // left to slice.
        const paginatedExperiments = computed(() => experiments.value || []);

        // Range of the current window, 1-based and inclusive. Derived from
        // the page and what the server actually returned, not from
        // experiments.length -- which is now one page.
        const experimentRangeStart = computed(() => {
            if (!experiments.value || experiments.value.length === 0) return 0;
            return (experimentPage.value - 1) * experimentPageSize + 1;
        });
        const experimentRangeEnd = computed(() =>
            experimentRangeStart.value === 0
                ? 0
                : experimentRangeStart.value + experiments.value.length - 1);

        const experimentFillerRows = computed(() => {
            if (experimentTotalPages.value <= 1) return 0;
            return Math.max(0, experimentPageSize - (experiments.value?.length || 0));
        });

        const goToExperimentPage = async (page) => {
            const target = Math.min(Math.max(1, page), experimentTotalPages.value);
            if (target === experimentPage.value) return;
            experimentPage.value = target;
            experimentLimit.value = experimentPageSize;
            await loadExperiments();
        };

        // Loading states
        const evaluatingExperiment = ref(null);

        // Theme
        const isDarkMode = ref(false);

        const initTheme = () => {
            // Check localStorage first, then system preference
            const savedTheme = localStorage.getItem('graflag-theme');
            if (savedTheme) {
                isDarkMode.value = savedTheme === 'dark';
            } else {
                isDarkMode.value = window.matchMedia('(prefers-color-scheme: dark)').matches;
            }
            applyTheme();
        };

        const applyTheme = () => {
            if (isDarkMode.value) {
                document.documentElement.setAttribute('data-theme', 'dark');
            } else {
                document.documentElement.setAttribute('data-theme', 'light');
            }
        };

        const toggleTheme = () => {
            isDarkMode.value = !isDarkMode.value;
            localStorage.setItem('graflag-theme', isDarkMode.value ? 'dark' : 'light');
            applyTheme();
        };

        // Initialize theme immediately
        initTheme();

        // Browser Notifications
        const notificationsEnabled = ref(false);
        const previousExperimentStates = ref({});
        const notificationReady = ref(false); // Prevents notifications on page load
        let webSocketSynced = false; // Track if first WebSocket update received

        const requestNotificationPermission = async () => {
            if (!('Notification' in window)) {
                console.log('Browser does not support notifications');
                return;
            }

            // Only auto-enable if user EXPLICITLY enabled via toggle before
            const savedPref = localStorage.getItem('graflag-notifications');
            if (savedPref === 'enabled' && Notification.permission === 'granted') {
                notificationsEnabled.value = true;
            } else {
                notificationsEnabled.value = false;
            }
        };

        const sendNotification = (title, body, icon = '') => {
            if (!notificationsEnabled.value) return;

            try {
                const notification = new Notification(title, {
                    body: body,
                    icon: '/static/favicon.ico',
                    badge: '/static/favicon.ico',
                    tag: title, // Prevents duplicate notifications
                    requireInteraction: false
                });

                // Auto-close after 5 seconds
                setTimeout(() => notification.close(), 5000);

                // Focus window when clicked
                notification.onclick = () => {
                    window.focus();
                    notification.close();
                };
            } catch (e) {
                console.error('Notification error:', e);
            }
        };

        const toggleNotifications = async () => {
            if (!('Notification' in window)) {
                alert('Your browser does not support notifications');
                return;
            }

            if (notificationsEnabled.value) {
                // Disable notifications
                notificationsEnabled.value = false;
                localStorage.setItem('graflag-notifications', 'disabled');
            } else {
                // Try to enable notifications
                if (Notification.permission === 'granted') {
                    notificationsEnabled.value = true;
                    localStorage.setItem('graflag-notifications', 'enabled');
                } else if (Notification.permission === 'denied') {
                    alert('Notifications are blocked. Please enable them in your browser settings.');
                } else {
                    const permission = await Notification.requestPermission();
                    if (permission === 'granted') {
                        notificationsEnabled.value = true;
                        localStorage.setItem('graflag-notifications', 'enabled');
                    }
                }
            }
        };

        const checkExperimentChanges = (newExperiments) => {
            const shouldNotify = notificationsEnabled.value && notificationReady.value;

            for (const exp of newExperiments) {
                const prevState = previousExperimentStates.value[exp.name];

                // Only send notifications if enabled and ready (after initial sync)
                if (shouldNotify && prevState) {
                    // Check for status transitions from running
                    if (prevState.status === 'running' && exp.status !== 'running') {
                        if (exp.status === 'completed') {
                            sendNotification(
                                '[OK] Experiment Completed',
                                `${exp.method} on ${exp.dataset} has finished successfully`
                            );
                        } else if (exp.status === 'failed') {
                            sendNotification(
                                '[FAIL] Experiment Failed',
                                `${exp.method} on ${exp.dataset} has failed`
                            );
                        } else if (exp.status === 'stopped') {
                            sendNotification(
                                '[STOP] Experiment Stopped',
                                `${exp.method} on ${exp.dataset} was stopped`
                            );
                        }
                    }

                    // Check for evaluation completed
                    if (!prevState.has_evaluation && exp.has_evaluation) {
                        sendNotification(
                            '[EVAL] Evaluation Completed',
                            `Evaluation for ${exp.method} on ${exp.dataset} is ready`
                        );
                    }
                }

                // Always update stored state (even during initial sync)
                previousExperimentStates.value[exp.name] = {
                    status: exp.status,
                    has_evaluation: exp.has_evaluation,
                    has_results: exp.has_results
                };
            }
        };

        // Request notification permission on load
        requestNotificationPermission();

        // API Methods
        // One resource per panel. Each carries its own state ('loading' |
        // 'ready' | 'error') and message, so the template can tell "asking",
        // "nothing there" and "could not ask" apart. Before this every loader
        // swallowed its error into an empty array and the UI showed the same
        // blank table for all three.
        const clusterRes = useResource('/api/cluster/info');
        const methodsRes = useResource('/api/methods', { initial: [] });
        const datasetsRes = useResource('/api/datasets', { initial: [] });
        const servicesRes = useResource('/api/services', { initial: [] });
        const experimentsRes = useResource('/api/experiments', { initial: [] });

        // One line at the top of the page when anything is failing. Panels
        // show their own error too, but a user whose cluster is unreachable
        // should not have to notice four empty tables to work that out.
        const connectionError = computed(() => {
            const failed = [
                ['cluster', clusterRes], ['methods', methodsRes],
                ['datasets', datasetsRes], ['services', servicesRes],
                ['experiments', experimentsRes],
            ].filter(([, r]) => r.state.value === 'error');
            if (failed.length === 0) return null;
            return {
                count: failed.length,
                panels: failed.map(([name]) => name).join(', '),
                message: failed[0][1].error.value,
            };
        });

        const loadClusterInfo = async (opts) => {
            await clusterRes.load({}, opts);
            // Keep the last good value on failure. Overwriting with null is
            // how a momentary blip became a header reading "0M 0W" over a
            // perfectly healthy five-node swarm.
            if (clusterRes.data.value) {
                clusterInfo.value = clusterRes.data.value;
            }
        };

        // Cluster status was fetched once, at mount, and never again -- so a
        // failure while the SSH tunnel was still cold (which is exactly when
        // the first request lands, right after a restart) left the header
        // showing a disconnected cluster until someone reloaded the page.
        // The endpoint is cached server-side for 30s, so re-asking costs
        // almost nothing; a failed attempt retries sooner than a good one.
        let clusterTimer = null;
        const scheduleClusterRefresh = () => {
            if (clusterTimer) clearTimeout(clusterTimer);
            const delay = clusterRes.state.value === 'error' ? 5000 : 20000;
            clusterTimer = setTimeout(async () => {
                await loadClusterInfo({ quiet: true });
                scheduleClusterRefresh();
            }, delay);
        };

        const loadMethods = async (opts) => {
            const data = await methodsRes.load({}, opts);
            if (Array.isArray(data)) methods.value = data;
        };

        const loadDatasets = async (opts) => {
            const data = await datasetsRes.load({}, opts);
            if (Array.isArray(data)) datasets.value = data;
        };

        const loadServices = async (opts) => {
            const data = await servicesRes.load({}, opts);
            if (Array.isArray(data)) services.value = data;
        };

        const loadExperiments = async (opts = {}) => {
            const params = { limit: experimentLimit.value,
                             offset: (experimentPage.value - 1) * experimentPageSize };
            const body = await experimentsRes.load(params, opts);
            if (!body) return;                    // error; experimentsRes.state says so
            // The API answers {items,total,limit,offset,truncated}. The bare
            // array is still accepted: the WebSocket updater broadcasts one,
            // and so would an older server.
            const items = Array.isArray(body) ? body : body.items;
            if (!Array.isArray(items)) return;
            checkExperimentChanges(items);
            experiments.value = items;
            experimentTotal.value = Array.isArray(body) ? null : body.total;
            experimentsTruncated.value = Array.isArray(body) ? false : !!body.truncated;
        };

        // "Load all" for the truncation notice: ask for everything the share
        // has rather than the default window. Cheap now that the endpoint is
        // cached server-side.
        const loadAllExperiments = async () => {
            experimentLimit.value = Math.max(experimentTotal.value || 0, 500);
            experimentPage.value = 1;
            await loadExperiments();
        };

        const stopExperiment = async (name) => {
            if (!confirm(`Stop experiment ${name}?`)) return;

            try {
                const res = await fetch(`/api/experiments/${name}/stop`, { method: 'POST' });
                const data = await res.json();
                if (data.success) {
                    loadExperiments();
                } else {
                    alert('[FAIL] Failed to stop experiment');
                }
            } catch (error) {
                alert('[FAIL] Error: ' + error.message);
            }
        };

        const deleteExperiment = async (name) => {
            if (!confirm(`Are you sure you want to delete experiment "${name}"?\n\nThis will permanently remove all results, logs, and evaluation data.`)) return;

            try {
                const res = await fetch(`/api/experiments/${name}/delete`, { method: 'POST' });
                const data = await res.json();
                if (data.success) {
                    loadExperiments();
                    loadServices();
                } else {
                    alert('[FAIL] Failed to delete experiment');
                }
            } catch (error) {
                alert('[FAIL] Error: ' + error.message);
            }
        };

        const evaluateExperiment = async (name) => {
            evaluatingExperiment.value = name;
            try {
                const res = await fetch(`/api/experiments/${name}/evaluate`, { method: 'POST' });
                const data = await res.json();
                if (data.success) {
                    // Poll for evaluation completion
                    const pollInterval = setInterval(async () => {
                        const expRes = await fetch(`/api/experiments/${name}`);
                        const expData = await expRes.json();
                        if (expData.has_evaluation) {
                            clearInterval(pollInterval);
                            evaluatingExperiment.value = null;
                            loadExperiments();
                        }
                    }, 2000);
                    // Timeout after 5 minutes
                    setTimeout(() => {
                        clearInterval(pollInterval);
                        if (evaluatingExperiment.value === name) {
                            evaluatingExperiment.value = null;
                        }
                    }, 300000);
                } else {
                    evaluatingExperiment.value = null;
                    alert('[FAIL] Error: ' + data.error);
                }
            } catch (error) {
                evaluatingExperiment.value = null;
                alert('[FAIL] Error: ' + error.message);
            }
        };
        
        let logPollingInterval = null;

        const clearLogPolling = () => {
            if (logPollingInterval) {
                // setTimeout now, not setInterval: the delay changes between
                // ticks, which an interval cannot express.
                clearTimeout(logPollingInterval);
                logPollingInterval = null;
            }
        };

        const viewLogs = async (name) => {
            clearLogPolling();
            viewMode.value = 'logs';
            viewExperiment.value = name;
            viewLogs_text.value = 'Loading logs...';
            viewLogsPaused.value = false;

            // Terminal experiments never emit another line, so polling them
            // is pure load on the manager. Anything else backs off when the
            // output stops changing: a long training run that logs once a
            // minute was being asked thirty times for the same bytes.
            const TERMINAL = ['completed', 'failed', 'stopped'];
            const MIN_DELAY = 2000;
            const MAX_DELAY = 30000;
            let delay = MIN_DELAY;
            let previous = null;

            const scheduleNext = () => {
                clearLogPolling();
                logPollingInterval = setTimeout(fetchLogs, delay);
            };

            const fetchLogs = async () => {
                if (viewLogsPaused.value || viewMode.value !== 'logs' || viewExperiment.value !== name) return;
                try {
                    const res = await fetch(`/api/experiments/${name}/logs?tail=200`);
                    if (!res.ok) throw new Error(`HTTP ${res.status}`);
                    const data = await res.json();
                    const text = data.logs.join('\n') || 'No logs available';
                    // Unchanged output means the run is quiet, not that it is
                    // gone: double the wait rather than stopping.
                    delay = (text === previous)
                        ? Math.min(delay * 2, MAX_DELAY)
                        : MIN_DELAY;
                    previous = text;
                    viewLogs_text.value = text;

                    const exp = (experiments.value || []).find(e => e.name === name);
                    if (exp && TERMINAL.includes(String(exp.status).toLowerCase())) {
                        clearLogPolling();      // finished: nothing more will arrive
                        return;
                    }
                } catch (error) {
                    viewLogs_text.value = `Error: ${error.message}`;
                    delay = Math.min(delay * 2, MAX_DELAY);   // back off on failure too
                }
                scheduleNext();
            };

            await fetchLogs();
        };

        const showEvaluation = async (name) => {
            clearLogPolling();
            viewMode.value = 'eval';
            viewExperiment.value = name;
            viewEval_data.value = null;
            viewResults_data.value = null;

            try {
                const [results, evaluation] = await Promise.all([
                    fetch(`/api/experiments/${name}/results`).then(r => r.ok ? r.json() : null),
                    fetch(`/api/experiments/${name}/evaluation`).then(r => r.ok ? r.json() : null)
                ]);
                viewResults_data.value = results;
                viewEval_data.value = evaluation;
            } catch (error) {
                viewEval_data.value = { error: error.message };
            }
        };

        const closeDetailPanel = () => {
            clearLogPolling();
            viewMode.value = null;
            viewExperiment.value = null;
            viewLogsPaused.value = false;
        };

        // Plot lightbox
        const lightboxIndex = ref(0);
        const lightboxOpen = ref(false);

        const openLightbox = (index) => {
            lightboxIndex.value = index;
            lightboxOpen.value = true;
        };
        const closeLightbox = () => { lightboxOpen.value = false; };
        const lightboxPrev = () => {
            if (!viewEval_data.value || !viewEval_data.value.plots_available) return;
            const len = viewEval_data.value.plots_available.length;
            lightboxIndex.value = (lightboxIndex.value - 1 + len) % len;
        };
        const lightboxNext = () => {
            if (!viewEval_data.value || !viewEval_data.value.plots_available) return;
            const len = viewEval_data.value.plots_available.length;
            lightboxIndex.value = (lightboxIndex.value + 1) % len;
        };
        const lightboxKeyHandler = (e) => {
            if (!lightboxOpen.value) return;
            if (e.key === 'Escape') closeLightbox();
            else if (e.key === 'ArrowLeft') lightboxPrev();
            else if (e.key === 'ArrowRight') lightboxNext();
        };
        window.addEventListener('keydown', lightboxKeyHandler);

        // Keep modal for backward compat but not primary use
        const closeModal = () => {
            showModal.value = false;
        };
        
        // Counts are counts: `num_samples` printed as 2751.0000 read as a
        // measurement. Everything else keeps four decimals, so an AUC of
        // exactly 1 still reads 1.0000 rather than 1.
        const COUNT_METRIC = /^(num_|total_)/;
        const formatMetric = (key, value) => {
            if (typeof value !== 'number') return value;
            if (COUNT_METRIC.test(key) || key === 'k' || key === 'temporal_span') {
                return String(Math.round(value));
            }
            return value.toFixed(4);
        };

        // Samples the evaluator left out (sentinel or non-finite scores),
        // from the `filtering` block it reports alongside the metrics.
        const excludedSamples = (metrics) => {
            const f = metrics && metrics.filtering;
            if (!f || typeof f !== 'object') return 0;
            const total = Number(f.total), kept = Number(f.kept);
            return Number.isFinite(total) && Number.isFinite(kept) ? total - kept : 0;
        };

        // WebSocket connection
        let socket = null;
        let updateTimeouts = {};
        let reconnectAttempts = 0;

        // Every live status on this page came from the Socket.IO push and
        // from nothing else: `disconnect` and `connect_error` only wrote to
        // the console, and socket.io gave up for good after five attempts.
        // So a dropped connection froze every badge on screen -- silently,
        // because the banner watches REST failures and the REST calls were
        // not being made -- until somebody reloaded the page. REST polling
        // is now the floor underneath the push: a slow safety net while the
        // socket is delivering, the primary source when it is not.
        //   'connecting' -> starting up, say nothing yet
        //   'live'       -> the push is working
        //   'polling'    -> no push; the page is refetching on a timer
        const liveState = ref('connecting');

        const LIVE_IDLE_MS = 30000;     // safety net; the push does the work
        const LIVE_FALLBACK_MS = 4000;  // the updater's own cadence, roughly
        let liveTimer = null;

        const refreshLiveData = () => Promise.allSettled([
            loadExperiments({ quiet: true }),
            loadServices({ quiet: true }),
        ]);

        const scheduleLiveRefresh = () => {
            if (liveTimer) clearTimeout(liveTimer);
            const delay = liveState.value === 'live' ? LIVE_IDLE_MS
                                                     : LIVE_FALLBACK_MS;
            liveTimer = setTimeout(async () => {
                // A hidden tab is not being read; polling it wakes the
                // manager for nothing. visibilitychange refetches on return.
                if (document.visibilityState !== 'hidden') {
                    await refreshLiveData();
                }
                scheduleLiveRefresh();
            }, delay);
        };

        // Coming back to a tab that sat in the background is the other way a
        // stale status gets read as a live one.
        const onVisibilityChange = () => {
            if (document.visibilityState !== 'visible') return;
            if (socket && !socket.connected) socket.connect();
            refreshLiveData();
            loadClusterInfo({ quiet: true });
            scheduleLiveRefresh();
        };
        
        const deepEqual = (obj1, obj2) => {
            if (obj1 === obj2) return true;
            if (obj1 == null || obj2 == null) return false;
            if (typeof obj1 !== 'object' || typeof obj2 !== 'object') return obj1 === obj2;
            
            const keys1 = Object.keys(obj1);
            const keys2 = Object.keys(obj2);
            
            if (keys1.length !== keys2.length) return false;
            
            for (let key of keys1) {
                if (!keys2.includes(key)) return false;
                if (!deepEqual(obj1[key], obj2[key])) return false;
            }
            return true;
        };
        
        const smartMerge = (current, incoming, keyField = 'name') => {
            if (!incoming || !Array.isArray(incoming)) return current;
            // An empty list used to be ignored here, because a dropped SSH
            // tunnel was reported as [] and the panel emptied at random.
            // That is fixed at the source now (api.list_running_services
            // raises), so [] means what it says -- and ignoring it left the
            // last service on screen forever once the cluster went quiet.
            
            const result = [];
            
            for (const newItem of incoming) {
                const key = newItem[keyField];
                const existing = current.find(item => item[keyField] === key);
                
                if (existing && deepEqual(existing, newItem)) {
                    result.push(existing);
                } else {
                    result.push(newItem);
                }
            }
            
            return result;
        };
        
        const debouncedUpdate = (type, data) => {
            if (updateTimeouts[type]) {
                clearTimeout(updateTimeouts[type]);
            }

            updateTimeouts[type] = setTimeout(() => {
                if (type === 'experiments_changed') {
                    // The server says the list moved; it no longer sends the
                    // list. It used to push a fixed limit=50 array which was
                    // assigned straight over whatever page was showing, so a
                    // dashboard asking for five rendered fifty. Refetch the
                    // window this client is actually on -- quiet, so the
                    // table does not blank on every tick.
                    if (data && typeof data.total === 'number') {
                        experimentTotal.value = data.total;
                    }
                    loadExperiments({ quiet: true });
                } else if (type === 'services') {
                    const merged = smartMerge(services.value, data);
                    if (!deepEqual(services.value, merged)) {
                        services.value = merged;
                    }
                }
            }, 100);
        };
        
        const connectWebSocket = () => {
            console.log('[WebSocket] Connecting...');
            
            socket = io({
                // Socket.IO's own default order, restored. Reversed, the
                // client opened with a websocket upgrade -- which the
                // werkzeug dev server cannot serve (it answers 500,
                // "write() before start_response") -- and then gave up
                // instead of falling back, so the dashboard had no live
                // connection at all and statuses only moved on a reload.
                // Long-polling connects against threading mode, and
                // socket.io upgrades to websocket by itself wherever a
                // server supports it.
                transports: ['polling', 'websocket'],
                reconnection: true,
                reconnectionDelay: 1000,
                reconnectionDelayMax: 5000,
                // socket.io's own default. Capped at five, a restart of the
                // GUI -- or any outage longer than about fifteen seconds --
                // left the page permanently without a live connection.
                reconnectionAttempts: Infinity
            });
            
            socket.on('connect', () => {
                console.log('[WebSocket] Connected');
                reconnectAttempts = 0;
                liveState.value = 'live';
                scheduleLiveRefresh();      // drop back to the idle cadence
                // Whatever moved while the push was down.
                refreshLiveData();
                // Request initial data
                socket.emit('request_update', { type: 'all' });
            });
            
            socket.on('update', (data) => {
                console.log('[WebSocket] Update received:', data.type);
                // The array check used to gate every event, from when all
                // of them carried a list. `experiments_changed` carries
                // {total: n} instead, so the guard silently dropped it and
                // no status ever moved without a reload.
                if (data.type && data.data !== undefined && data.data !== null) {
                    debouncedUpdate(data.type, data.data);
                }
                // Mark WebSocket as synced after first experiments update
                if (data.type === 'experiments_changed' && !webSocketSynced) {
                    webSocketSynced = true;
                    console.log('WebSocket synced, notifications can now be enabled');
                }
            });
            
            socket.on('disconnect', (reason) => {
                console.log('[WebSocket] Disconnected:', reason);
                liveState.value = 'polling';
                scheduleLiveRefresh();      // take over now, not in 30s
                // socket.io reconnects by itself except when the server
                // closed the connection deliberately -- a GUI restart.
                if (reason === 'io server disconnect') socket.connect();
            });
            
            socket.on('connect_error', (error) => {
                console.error('[WebSocket] Connection error:', error);
                reconnectAttempts++;
                liveState.value = 'polling';
                scheduleLiveRefresh();
            });
            
            socket.on('error', (error) => {
                console.error('[WebSocket] Error:', error);
            });
        };
        
        // Lifecycle
        onMounted(async () => {
            const startTime = Date.now();
            console.log('Starting to load data...');
            
            try {
                const results = await Promise.allSettled([
                    loadClusterInfo(),
                    loadMethods(),
                    loadDatasets(),
                    loadExperiments(),
                    loadServices()
                ]);

                // Self-healing cluster status: without this a failure on the
                // line above was permanent until a page reload.
                scheduleClusterRefresh();
                
                // Log any failures
                results.forEach((result, index) => {
                    if (result.status === 'rejected') {
                        const names = ['cluster info', 'methods', 'datasets', 'experiments', 'services'];
                        console.error(`Failed to load ${names[index]}:`, result.reason);
                    }
                });
                
                console.log('All data loaded successfully');
            } catch (error) {
                console.error('Error loading initial data:', error);
            } finally {
                // Ensure minimum loading time of 500ms for smooth UX
                const elapsed = Date.now() - startTime;
                const minLoadTime = 500;
                
                if (elapsed < minLoadTime) {
                    await new Promise(resolve => setTimeout(resolve, minLoadTime - elapsed));
                }
                
                isLoading.value = false;
                console.log('UI ready, connecting WebSocket...');
                connectWebSocket();

                // The REST floor starts now, not when the socket fails: if
                // the push never connects at all there is no failure event
                // to react to, only silence.
                scheduleLiveRefresh();
                document.addEventListener('visibilitychange', onVisibilityChange);

                // The experiments table is inline markup, not a DataTable,
                // so it needs the handles attached by hand.
                nextTick(() => {
                    const t = document.querySelector(
                        '.experiments-table-side table');
                    if (t) enableColumnResize(t, 'experiments');
                });

                // Enable notifications only after WebSocket has synced (check every 500ms, max 10s)
                let checkCount = 0;
                const enableNotificationsWhenReady = () => {
                    checkCount++;
                    if (webSocketSynced) {
                        // Add small delay after sync to ensure states are fully updated
                        setTimeout(() => {
                            notificationReady.value = true;
                            console.log('Notifications ready (WebSocket synced)');
                        }, 500);
                    } else if (checkCount < 20) {
                        // Keep checking until WebSocket syncs or timeout
                        setTimeout(enableNotificationsWhenReady, 500);
                    } else {
                        // Fallback: enable after 10s even if WebSocket didn't sync
                        notificationReady.value = true;
                        console.log('Notifications ready (timeout fallback)');
                    }
                };
                setTimeout(enableNotificationsWhenReady, 1000); // Start checking after 1s
            }
        });
        
        return {
            isLoading,
            clusterInfo,
            methods,
            datasets,
            experiments,
            experimentTotal,
            experimentsTruncated,
            experimentFillerRows,
        experimentRangeStart,
        experimentRangeEnd,
        experimentLimit,
            goToExperimentPage,
            loadAllExperiments,
            // Per-panel state, so a template can say 'could not reach the
            // cluster' instead of rendering an empty table.
            connectionError,
            formatMetric,
            excludedSamples,
            // 'live' | 'polling' | 'connecting' -- the banner tells the user
            // when statuses are arriving on a timer rather than a push.
            liveState,
        clusterRes,
            methodsRes,
            datasetsRes,
            servicesRes,
            experimentsRes,
            services,
            showModal,
            modalContent,
            experimentPage,
            experimentTotalPages,
            paginatedExperiments,
            methodPage,
            datasetPage,
            servicePage,
            loadExperiments,
            stopExperiment,
            deleteExperiment,
            evaluateExperiment,
            evaluatingExperiment,
            viewLogs,
            showEvaluation,
            closeModal,
            isDarkMode,
            toggleTheme,
            notificationsEnabled,
            toggleNotifications,
            viewMode,
            viewExperiment,
            viewLogs_text,
            viewEval_data,
            viewResults_data,
            viewLogsPaused,
            closeDetailPanel,
            lightboxOpen,
            lightboxIndex,
            openLightbox,
            closeLightbox,
            lightboxPrev,
            lightboxNext
        };
    }
}).mount('#app');
