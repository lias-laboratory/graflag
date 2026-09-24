#!/usr/bin/env python3
"""
GraFlag GUI Server

A simple web-based GUI for GraFlag using Flask.
Usage: python graflag_gui.py serve
"""

import argparse
import logging
import os
import secrets
from pathlib import Path

from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit
from graflag.api import GraFlagAPI, GraFlagError
from graflag.core import GraFlag
from graflag.ssh import remote_path
from graflag.utils import valid_name
import json
import time
import re
import threading
from datetime import datetime

app = Flask(__name__,
            template_folder=str(Path(__file__).parent / 'templates'),
            static_folder=str(Path(__file__).parent / 'static'))
# A secret committed to the repository signs every deployment's sessions with
# a value anyone can read. Generated per process unless GRAFLAG_SECRET_KEY is
# set; nothing here persists sessions across a restart, so a fresh key costs
# nothing.
logger = logging.getLogger(__name__)

app.config['SECRET_KEY'] = os.environ.get(
    'GRAFLAG_SECRET_KEY') or secrets.token_hex(32)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')
api = None

# State tracking for change detection
last_state = {'experiments': None, 'services': None}
state_lock = threading.Lock()

# Server-side cache for methods and datasets (they don't change often)
cache = {
    'methods': {'data': None, 'timestamp': 0},
    'datasets': {'data': None, 'timestamp': 0},
    # Measured: /api/experiments cost 437ms warm and /api/cluster/info 240ms,
    # on every request, while methods and datasets were served from cache in
    # 0.8ms. Those two are the ones the dashboard polls, so the caching was
    # on the cheap static endpoints and absent from the hot path.
    'experiments': {'data': None, 'timestamp': 0,
                    'limit': None, 'offset': None},
    'cluster': {'data': None, 'timestamp': 0},
}
CACHE_TTL = 30          # methods and datasets: they change when a file lands
EXPERIMENTS_TTL = 2     # matches the updater's tick; anything longer would
                        # show a dashboard state older than its own refresh
cache_lock = threading.Lock()

# Socket.IO session ids currently connected. The background updater polls the
# manager every two seconds; without this it did so whether or not anyone was
# looking, forever, for a dashboard nobody had open.
connected_clients = set()


def _experiment_total():
    """Total on the share, or None when it cannot be determined."""
    try:
        return int(api.count_experiments())
    except Exception:
        return None


def should_poll() -> bool:
    """True when at least one dashboard is connected."""
    return bool(connected_clients)


def invalidate_experiments_cache():
    """Drop the experiment list after a write.

    A run, stop, evaluate or delete changes the list. Serving the pre-write
    cache for the rest of the TTL would show a state that no longer exists --
    most visibly, a deleted experiment reappearing for two seconds.
    """
    with cache_lock:
        cache['experiments']['data'] = None
        cache['experiments']['timestamp'] = 0


# ============================================================================
# Routes
# ============================================================================

# ============================================================================
# Input validation
# ============================================================================

# Experiment / method / dataset names are interpolated into shell commands that
# run on the swarm manager as root, so anything reaching those paths must be a
# plain identifier. Rejects quotes, ';', '$', backticks, spaces and path
# separators. The rule lives in graflag.utils, shared with the MCP server.
_valid_name = valid_name


def _reject(name: str):
    """Standard 400 for a rejected identifier."""
    return jsonify({'error': f'Invalid name: {name!r}'}), 400


@app.route('/')
def index():
    """Main dashboard page."""
    return render_template('index.html')


# ============================================================================
# WebSocket Events
# ============================================================================

@socketio.on('connect')
def handle_connect():
    """Handle client connection."""
    connected_clients.add(request.sid)
    print('[WebSocket] Client connected')
    emit('connected', {'status': 'connected'})

@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection."""
    connected_clients.discard(request.sid)
    print('[WebSocket] Client disconnected')

@socketio.on('request_update')
def handle_request_update(data):
    """Handle client request for updates.

    `data` is whatever the client sent, and the server has no authentication,
    so it is not necessarily a dict: a client emitting a string, a number, a
    list or null used to take this handler down with
    `AttributeError: 'str' object has no attribute 'get'`. Anything that is
    not a mapping is treated as a request for everything, which is what an
    omitted type already meant.
    """
    update_type = data.get('type', 'all') if isinstance(data, dict) else 'all'
    print(f'[WebSocket] Update requested: {update_type}')
    
    if update_type in ['all', 'experiments']:
        try:
            # A notification, not the list. Pushing a fixed limit=50 array
            # here overwrote whatever page the client was showing -- the
            # dashboard asked for five and rendered fifty, from the first
            # render, because this fires on connect. The client refetches its
            # own window, which is cached server-side and costs ~2ms.
            emit('update', {'type': 'experiments_changed',
                            'data': {'total': _experiment_total()}})
        except Exception as e:
            print(f'[ERROR] Failed to signal experiments: {e}')
    
    if update_type in ['all', 'services']:
        try:
            services = api.list_running_services()
            emit('update', {'type': 'services', 'data': services})
        except Exception as e:
            print(f'[ERROR] Failed to fetch services: {e}')


@app.route('/api/cluster/info')
def cluster_info():
    """Cluster status, cached briefly.

    Measured at 240ms per call and served uncached while the static endpoints
    were cached to 0.8ms. Nothing about a swarm's node list changes fast
    enough to need a round trip per request.
    """
    try:
        now = time.time()
        with cache_lock:
            entry = cache['cluster']
            if entry['data'] is not None and (now - entry['timestamp']) < CACHE_TTL:
                return jsonify(entry['data'])
        info = api.get_cluster_info()
        result = info.to_dict() if hasattr(info, 'to_dict') else info
        with cache_lock:
            cache['cluster'].update(data=result, timestamp=now)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/methods')
def list_methods():
    """List available methods."""
    try:
        # Check cache first
        now = time.time()
        if cache['methods']['data'] and (now - cache['methods']['timestamp']) < CACHE_TTL:
            return jsonify(cache['methods']['data'])
        
        # Fetch fresh data
        methods = api.list_methods()
        result = [m.to_dict() for m in methods]
        
        # Update cache
        cache['methods']['data'] = result
        cache['methods']['timestamp'] = now
        
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/methods/<method_name>')
def get_method(method_name):
    """Get specific method details."""
    if not _valid_name(method_name):
        return _reject(method_name)
    try:
        method = api.get_method_details(method_name)
        if method:
            return jsonify(method.to_dict())
        else:
            return jsonify({'error': 'Method not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/datasets')
def list_datasets():
    """List available datasets."""
    try:
        # Check cache first
        now = time.time()
        if cache['datasets']['data'] and (now - cache['datasets']['timestamp']) < CACHE_TTL:
            return jsonify(cache['datasets']['data'])
        
        # Fetch fresh data
        datasets = api.list_datasets()
        result = [d.to_dict() for d in datasets]
        
        # Update cache
        cache['datasets']['data'] = result
        cache['datasets']['timestamp'] = now
        
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/experiments')
def list_experiments():
    """List experiments.

    A negative `limit` is rejected rather than forwarded. It used to reach
    `list_experiments(limit=-1)` and become the Python slice `[:-1]`, which
    returns every experiment but the last -- 77 of 78 on this cluster, with
    nothing anywhere saying one was dropped.
    """
    try:
        limit = request.args.get('limit', 50, type=int)
        offset = request.args.get('offset', 0, type=int)
        if limit is None or limit < 0:
            return jsonify({'error': f'limit must be >= 0, got {limit}'}), 400
        if offset is None or offset < 0:
            return jsonify({'error': f'offset must be >= 0, got {offset}'}), 400

        now = time.time()
        with cache_lock:
            entry = cache['experiments']
            # Keyed on the window as well as the age. On the limit alone,
            # ?limit=100 was answered with the 50 already cached; without the
            # offset, page 3 would be answered with page 1.
            fresh = (entry['data'] is not None
                     and entry['limit'] == limit
                     and entry['offset'] == offset
                     and (now - entry['timestamp']) < EXPERIMENTS_TTL)
            if fresh:
                return jsonify(entry['data'])

        experiments = api.list_experiments(limit=limit, offset=offset)
        items = [e.to_dict() for e in experiments]

        # The list alone cannot say whether it is complete. Without this a
        # dashboard showing 50 of 78 looked exactly like one showing all 78.
        # The count is a second remote call, so a failure degrades to "total
        # unknown" rather than costing the caller the list it asked for.
        try:
            # int() inside the guard on purpose: the comparison below is what
            # actually fails on a non-numeric count, and leaving it outside
            # turned "the total is unavailable" into a 500 that cost the
            # caller the list as well.
            total = int(api.count_experiments())
        except Exception as exc:
            logger.debug(f"[INFO] Could not count experiments: {exc}")
            total = None

        payload = {
            'items': items,
            'total': total,
            'limit': limit,
            'offset': offset,
            'truncated': bool(total is not None
                              and total > offset + len(items)),
        }
        with cache_lock:
            cache['experiments'].update(
                data=payload, timestamp=now, limit=limit, offset=offset)
        return jsonify(payload)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/experiments/<experiment_name>')
def experiment_details(experiment_name):
    """Get experiment details."""
    if not _valid_name(experiment_name):
        return _reject(experiment_name)
    try:
        exp = api.get_experiment_details(experiment_name)
        if exp:
            return jsonify(exp.to_dict())
        else:
            return jsonify({'error': 'Experiment not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/experiments/<experiment_name>/results')
def experiment_results(experiment_name):
    """Get experiment results."""
    if not _valid_name(experiment_name):
        return _reject(experiment_name)
    try:
        results = api.get_experiment_results(experiment_name)
        if results:
            return jsonify(results.to_dict())
        else:
            return jsonify({'error': 'Results not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/experiments/<experiment_name>/evaluation')
def experiment_evaluation(experiment_name):
    """Get experiment evaluation results."""
    if not _valid_name(experiment_name):
        return _reject(experiment_name)
    try:
        evaluation = api.get_evaluation_results(experiment_name)
        if evaluation:
            return jsonify(evaluation.to_dict())
        else:
            return jsonify({'error': 'Evaluation not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/experiments/<experiment_name>/plot/<plot_name>')
def experiment_plot(experiment_name, plot_name):
    """Serve evaluation plot images."""
    if not _valid_name(experiment_name):
        return _reject(experiment_name)
    from flask import send_file
    import io
    import base64

    # Validate plot_name to prevent path traversal
    # Only allow alphanumeric, underscore, hyphen, and .png extension
    if not re.match(r'^[a-zA-Z0-9_-]+\.png$', plot_name):
        return jsonify({'error': 'Invalid plot name'}), 400

    try:
        # Get the plot file from remote server via SSH using base64 encoding
        plot_path = remote_path(
            api.config.remote_shared_dir, "experiments", experiment_name,
            "eval", plot_name,
        )

        # Read the file content via SSH with base64 encoding
        result = api.core.ssh.execute(f"base64 {plot_path} 2>/dev/null")

        if result.returncode != 0 or not result.stdout.strip():
            return jsonify({'error': 'Plot not found'}), 404

        # Decode base64 to binary
        image_data = base64.b64decode(result.stdout.strip())

        # Return the image
        return send_file(
            io.BytesIO(image_data),
            mimetype='image/png',
            as_attachment=False,
            download_name=plot_name
        )
    except Exception as e:
        print(f"[ERROR] Failed to serve plot {plot_name} for {experiment_name}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/services')
def list_services():
    """List running services."""
    try:
        services = api.list_running_services()
        return jsonify(services)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/run', methods=['POST'])
def submit_run():
    """Run an experiment."""
    try:
        data = request.json
        method = data.get('method')
        dataset = data.get('dataset')
        build = data.get('build', False)
        gpu = data.get('gpu', True)
        params = data.get('params', {})

        if not method or not dataset:
            return jsonify({'error': 'Method and dataset are required'}), 400
        if not _valid_name(method):
            return _reject(method)
        if not _valid_name(dataset):
            return _reject(dataset)

        print(f"[DEBUG] Starting run: method={method}, dataset={dataset}, build={build}, gpu={gpu}")

        # Build the name here and hand it to run(), so the name returned to the
        # browser is the directory that actually gets created (run() lowercases
        # and would otherwise pick its own timestamp).
        exp_name = GraFlag.make_experiment_name(method, dataset)

        # Run in background thread to avoid blocking
        def run_in_background():
            try:
                api.run(
                    method=method,
                    dataset=dataset,
                    build=build,
                    gpu=gpu,
                    method_params=params,
                    exp_name=exp_name,
                )
            except Exception as e:
                print(f"[ERROR] Background run error: {e}")

        invalidate_experiments_cache()
        thread = threading.Thread(target=run_in_background, daemon=True)
        thread.start()

        print(f"[DEBUG] Run started in background: {exp_name}")

        return jsonify({'experiment_name': exp_name})

    except GraFlagError as e:
        print(f"[ERROR] GraFlagError in run: {e}")
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        print(f"[ERROR] Unexpected error in run: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/experiments/<experiment_name>/evaluate', methods=['POST'])
def evaluate_experiment(experiment_name):
    """Run evaluation on an experiment."""
    if not _valid_name(experiment_name):
        return _reject(experiment_name)
    try:
        print(f"[DEBUG] Starting evaluation for {experiment_name}")
        
        # Run evaluation in background to avoid blocking
        def run_evaluation():
            try:
                api.evaluate_experiment(experiment_name)
                print(f"[DEBUG] Evaluation completed for {experiment_name}")
            except Exception as e:
                print(f"[ERROR] Evaluation error for {experiment_name}: {e}")
        
        invalidate_experiments_cache()
        thread = threading.Thread(target=run_evaluation, daemon=True)
        thread.start()
        
        return jsonify({'success': True, 'message': 'Evaluation started'})
    except GraFlagError as e:
        print(f"[ERROR] GraFlagError in evaluate: {e}")
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        print(f"[ERROR] Unexpected error in evaluate: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/experiments/<experiment_name>/stop', methods=['POST'])
def stop_experiment(experiment_name):
    """Stop a running experiment."""
    if not _valid_name(experiment_name):
        return _reject(experiment_name)
    try:
        print(f"[DEBUG] Stopping experiment {experiment_name}")
        
        # Run stop in background to avoid blocking
        def stop_in_background():
            try:
                success = api.stop_experiment(experiment_name)
                print(f"[DEBUG] Stop result for {experiment_name}: {success}")
            except Exception as e:
                print(f"[ERROR] Stop error for {experiment_name}: {e}")
        
        invalidate_experiments_cache()
        thread = threading.Thread(target=stop_in_background, daemon=True)
        thread.start()
        
        return jsonify({'success': True, 'message': 'Stop request sent'})
    except Exception as e:
        print(f"[ERROR] Unexpected error in stop: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/experiments/<experiment_name>/delete', methods=['POST'])
def delete_experiment(experiment_name):
    """Delete an experiment (stop service + remove directory)."""
    if not _valid_name(experiment_name):
        return _reject(experiment_name)
    try:
        print(f"[DEBUG] Deleting experiment {experiment_name}")

        def delete_in_background():
            try:
                success = api.delete_experiment(experiment_name)
                print(f"[DEBUG] Delete result for {experiment_name}: {success}")
            except Exception as e:
                print(f"[ERROR] Delete error for {experiment_name}: {e}")

        invalidate_experiments_cache()
        thread = threading.Thread(target=delete_in_background, daemon=True)
        thread.start()

        return jsonify({'success': True, 'message': 'Delete request sent'})
    except Exception as e:
        print(f"[ERROR] Unexpected error in delete: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/experiments/<experiment_name>/logs')
def experiment_logs(experiment_name):
    """Get experiment logs."""
    if not _valid_name(experiment_name):
        return _reject(experiment_name)
    try:
        tail = request.args.get('tail', 100, type=int)
        print(f"[DEBUG] Fetching logs for {experiment_name}, tail={tail}")
        logs = api.get_experiment_logs(experiment_name, tail=tail)
        print(f"[DEBUG] Retrieved logs type: {type(logs)}, length: {len(logs) if logs else 0}")
        if logs:
            print(f"[DEBUG] First few log entries: {logs[:3]}")
            print(f"[DEBUG] Are logs empty strings? {all(not line or not line.strip() for line in logs)}")
        return jsonify({'logs': logs if logs else []})
    except Exception as e:
        print(f"[ERROR] Failed to get logs for {experiment_name}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e), 'logs': []}), 500


# ============================================================================
# Broadcasting
# ============================================================================

def broadcast_update(event_type, data):
    """Broadcast an update to all connected clients via WebSocket."""
    try:
        socketio.emit('update', {
            'type': event_type,
            'data': data,
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        print(f'[ERROR] Failed to broadcast {event_type}: {e}')


def background_updater():
    """Background thread to periodically check for updates."""
    global last_state
    
    def fetch_and_broadcast_experiments():
        """Tell clients the experiment list moved; do not send it.

        This used to broadcast `list_experiments(limit=50)` as a bare array,
        which the client assigned straight into the list it was rendering --
        so a dashboard paging five at a time was handed fifty every two
        seconds and pagination could not survive a tick. The change signal is
        a fingerprint; each client refetches the window it is actually
        showing, from the server-side cache.
        """
        try:
            experiments = api.list_experiments(limit=50)
            fingerprint = [
                (e.name, getattr(e, 'status', None)) for e in experiments
            ]
            with state_lock:
                if fingerprint != last_state['experiments']:
                    changed = True
                    last_state['experiments'] = fingerprint
                else:
                    changed = False
            if changed:
                # Invalidate before announcing. The client answers this
                # signal by refetching, and the REST response is cached for
                # EXPERIMENTS_TTL -- so without this it was handed the very
                # data the updater had just decided was out of date, and a
                # status change did not appear until the cache aged out.
                invalidate_experiments_cache()
                broadcast_update('experiments_changed',
                                 {'total': _experiment_total()})
        except Exception as e:
            print(f"[ERROR] Failed to fetch experiments: {e}")
    
    def fetch_and_broadcast_services():
        """Fetch services and broadcast if changed."""
        try:
            services = api.list_running_services()
            
            with state_lock:
                # Only broadcast if changed
                if services != last_state['services']:
                    broadcast_update('services', services)
                    last_state['services'] = services
        except Exception as e:
            print(f"[ERROR] Failed to fetch services: {e}")
    
    while True:
        try:
            time.sleep(2)  # Check every 2 seconds

            # Nothing to broadcast to, nothing worth asking the manager for.
            # Without this the loop polled every two seconds for as long as
            # the process lived, whether or not a dashboard was open.
            if not should_poll():
                continue

            # Fetch experiments and services in parallel, then *wait*. The
            # previous version started two threads per tick and looped
            # immediately: a fetch slower than the 2s tick -- a bigger cluster
            # or a slower link -- piled threads up without bound, each opening
            # its own work. Joining makes a slow cluster slow the polling
            # rather than multiply it.
            exp_thread = threading.Thread(target=fetch_and_broadcast_experiments, daemon=True)
            svc_thread = threading.Thread(target=fetch_and_broadcast_services, daemon=True)

            exp_thread.start()
            svc_thread.start()
            exp_thread.join(timeout=30)
            svc_thread.join(timeout=30)

        except Exception as e:
            print(f"Background updater error: {e}")
            time.sleep(5)


# ============================================================================
# CLI
# ============================================================================

def serve(config_file, host, port, debug):
    """Start the web server."""
    global api
    from graflag.config import get_config_path

    config_path = get_config_path(config_file)
    print("[INFO] Starting GraFlag GUI Server...")
    print(f"   Config: {config_path}")
    print(f"   URL: http://{host}:{port}")

    api = GraFlagAPI(config_file=str(config_path))
    
    # Start background updater
    updater_thread = threading.Thread(target=background_updater, daemon=True)
    updater_thread.start()
    
    # Start Flask with SocketIO
    socketio.run(app, host=host, port=port, debug=debug, allow_unsafe_werkzeug=True)


def main():
    """Start the GraFlag GUI server."""
    parser = argparse.ArgumentParser(description="GraFlag GUI Server")
    parser.add_argument('--config', default='.env', help='Path to config file')
    parser.add_argument('--host', default='0.0.0.0', help='Host to bind to')
    parser.add_argument('--port', default=5000, type=int, help='Port to bind to')
    parser.add_argument('--debug', action='store_true', help='Enable debug mode')
    args = parser.parse_args()
    serve(args.config, args.host, args.port, args.debug)


if __name__ == '__main__':
    main()
