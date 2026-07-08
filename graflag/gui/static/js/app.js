import ClusterStatus from './components/ClusterStatus.js';
import RunForm from './components/RunForm.js';
import DataTable from './components/DataTable.js';
import ExperimentModal from './components/ExperimentModal.js';

const { createApp, ref, computed, onMounted } = Vue;

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
            if (!experiments.value || experiments.value.length === 0) return 1;
            return Math.ceil(experiments.value.length / 5);
        });
        const paginatedExperiments = computed(() => {
            const start = (experimentPage.value - 1) * 5;
            return experiments.value.slice(start, start + 5);
        });

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

        const sendNotification = (title, body, icon = '⚡') => {
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
                                '✅ Experiment Completed',
                                `${exp.method} on ${exp.dataset} has finished successfully`
                            );
                        } else if (exp.status === 'failed') {
                            sendNotification(
                                '❌ Experiment Failed',
                                `${exp.method} on ${exp.dataset} has failed`
                            );
                        } else if (exp.status === 'stopped') {
                            sendNotification(
                                '⏹️ Experiment Stopped',
                                `${exp.method} on ${exp.dataset} was stopped`
                            );
                        }
                    }

                    // Check for evaluation completed
                    if (!prevState.has_evaluation && exp.has_evaluation) {
                        sendNotification(
                            '📊 Evaluation Completed',
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
        const loadClusterInfo = async () => {
            try {
                const res = await fetch('/api/cluster/info');
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();
                if (data) {
                    clusterInfo.value = data;
                    console.log('Loaded cluster info:', data);
                }
            } catch (error) {
                console.error('Error loading cluster info:', error);
                clusterInfo.value = null;
            }
        };
        
        const loadMethods = async () => {
            try {
                const res = await fetch('/api/methods');
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();
                if (Array.isArray(data)) {
                    methods.value = data;
                    console.log('Loaded methods:', data.length);
                }
            } catch (error) {
                console.error('Error loading methods:', error);
                methods.value = [];
            }
        };
        
        const loadDatasets = async () => {
            try {
                const res = await fetch('/api/datasets');
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();
                if (Array.isArray(data)) {
                    datasets.value = data;
                    console.log('Loaded datasets:', data.length);
                }
            } catch (error) {
                console.error('Error loading datasets:', error);
                datasets.value = [];
            }
        };
        
        const loadExperiments = async () => {
            try {
                const res = await fetch('/api/experiments');
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();
                if (Array.isArray(data)) {
                    // Update states and check for changes (notifications only after notificationReady)
                    checkExperimentChanges(data);
                    experiments.value = data;
                    console.log('Loaded experiments:', data.length);
                }
            } catch (error) {
                console.error('Error loading experiments:', error);
                experiments.value = [];
            }
        };
        
        const loadServices = async () => {
            try {
                const res = await fetch('/api/services');
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();
                if (Array.isArray(data)) {
                    services.value = data;
                    console.log('Loaded services:', data.length);
                }
            } catch (error) {
                console.error('Error loading services:', error);
                services.value = [];
            }
        };
        
        const stopExperiment = async (name) => {
            if (!confirm(`Stop experiment ${name}?`)) return;

            try {
                const res = await fetch(`/api/experiments/${name}/stop`, { method: 'POST' });
                const data = await res.json();
                if (data.success) {
                    loadExperiments();
                } else {
                    alert('❌ Failed to stop experiment');
                }
            } catch (error) {
                alert('❌ Error: ' + error.message);
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
                    alert('❌ Failed to delete experiment');
                }
            } catch (error) {
                alert('❌ Error: ' + error.message);
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
                    alert('❌ Error: ' + data.error);
                }
            } catch (error) {
                evaluatingExperiment.value = null;
                alert('❌ Error: ' + error.message);
            }
        };
        
        let logPollingInterval = null;

        const clearLogPolling = () => {
            if (logPollingInterval) {
                clearInterval(logPollingInterval);
                logPollingInterval = null;
            }
        };

        const viewLogs = async (name) => {
            clearLogPolling();
            viewMode.value = 'logs';
            viewExperiment.value = name;
            viewLogs_text.value = 'Loading logs...';
            viewLogsPaused.value = false;

            const fetchLogs = async () => {
                if (viewLogsPaused.value || viewMode.value !== 'logs' || viewExperiment.value !== name) return;
                try {
                    const res = await fetch(`/api/experiments/${name}/logs?tail=200`);
                    const data = await res.json();
                    viewLogs_text.value = data.logs.join('\n') || 'No logs available';
                } catch (error) {
                    viewLogs_text.value = `Error: ${error.message}`;
                }
            };

            await fetchLogs();
            logPollingInterval = setInterval(fetchLogs, 2000);
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
        
        // WebSocket connection
        let socket = null;
        let updateTimeouts = {};
        let reconnectAttempts = 0;
        const maxReconnectAttempts = 5;
        
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
            if (incoming.length === 0 && current.length > 0) return current;
            
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
                if (type === 'experiments') {
                    // Update states and check for changes (notifications only after notificationReady)
                    checkExperimentChanges(data);
                    const merged = smartMerge(experiments.value, data);
                    if (!deepEqual(experiments.value, merged)) {
                        experiments.value = merged;
                    }
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
                transports: ['websocket', 'polling'],
                reconnection: true,
                reconnectionDelay: 1000,
                reconnectionDelayMax: 5000,
                reconnectionAttempts: maxReconnectAttempts
            });
            
            socket.on('connect', () => {
                console.log('[WebSocket] Connected');
                reconnectAttempts = 0;
                // Request initial data
                socket.emit('request_update', { type: 'all' });
            });
            
            socket.on('update', (data) => {
                console.log('[WebSocket] Update received:', data.type);
                if (data.type && data.data && Array.isArray(data.data)) {
                    debouncedUpdate(data.type, data.data);
                }
                // Mark WebSocket as synced after first experiments update
                if (data.type === 'experiments' && !webSocketSynced) {
                    webSocketSynced = true;
                    console.log('WebSocket synced, notifications can now be enabled');
                }
            });
            
            socket.on('disconnect', (reason) => {
                console.log('[WebSocket] Disconnected:', reason);
            });
            
            socket.on('connect_error', (error) => {
                console.error('[WebSocket] Connection error:', error);
                reconnectAttempts++;
                if (reconnectAttempts >= maxReconnectAttempts) {
                    console.error('[WebSocket] Max reconnection attempts reached');
                }
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
