#!/usr/bin/env python3
"""
GraFlag GUI Server

A simple web-based GUI for GraFlag using Flask.
Usage: python graflag_gui.py serve
"""

import argparse
from pathlib import Path

from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit
from graflag.api import GraFlagAPI, GraFlagError
import json
import time
import threading
from datetime import datetime

app = Flask(__name__,
            template_folder=str(Path(__file__).parent / 'templates'),
            static_folder=str(Path(__file__).parent / 'static'))
app.config['SECRET_KEY'] = 'graflag-secret-key'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')
api = None

# State tracking for change detection
last_state = {'experiments': None, 'services': None}
state_lock = threading.Lock()

# Server-side cache for methods and datasets (they don't change often)
cache = {
    'methods': {'data': None, 'timestamp': 0},
    'datasets': {'data': None, 'timestamp': 0}
}
CACHE_TTL = 30  # Cache for 30 seconds


# ============================================================================
# Routes
# ============================================================================

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
    print('[WebSocket] Client connected')
    emit('connected', {'status': 'connected'})

@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection."""
    print('[WebSocket] Client disconnected')

@socketio.on('request_update')
def handle_request_update(data):
    """Handle client request for updates."""
    update_type = data.get('type', 'all')
    print(f'[WebSocket] Update requested: {update_type}')
    
    if update_type in ['all', 'experiments']:
        try:
            experiments = api.list_experiments(limit=50)
            emit('update', {'type': 'experiments', 'data': [e.to_dict() for e in experiments]})
        except Exception as e:
            print(f'[ERROR] Failed to fetch experiments: {e}')
    
    if update_type in ['all', 'services']:
        try:
            services = api.list_running_services()
            emit('update', {'type': 'services', 'data': services})
        except Exception as e:
            print(f'[ERROR] Failed to fetch services: {e}')


@app.route('/api/cluster/info')
def cluster_info():
    """Get cluster information."""
    try:
        cluster = api.get_cluster_info()
        return jsonify(cluster.to_dict())
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
    """List experiments."""
    try:
        limit = request.args.get('limit', 50, type=int)
        experiments = api.list_experiments(limit=limit)
        return jsonify([e.to_dict() for e in experiments])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/experiments/<experiment_name>')
def experiment_details(experiment_name):
    """Get experiment details."""
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
    from flask import send_file
    import io
    import base64
    import re

    # Validate plot_name to prevent path traversal
    # Only allow alphanumeric, underscore, hyphen, and .png extension
    if not re.match(r'^[a-zA-Z0-9_-]+\.png$', plot_name):
        return jsonify({'error': 'Invalid plot name'}), 400

    try:
        # Get the plot file from remote server via SSH using base64 encoding
        plot_path = f"{api.config.remote_shared_dir}/experiments/{experiment_name}/eval/{plot_name}"

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

        print(f"[DEBUG] Starting run: method={method}, dataset={dataset}, build={build}, gpu={gpu}")

        # Generate experiment name first
        from datetime import datetime
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        exp_name = f"exp__{method}__{dataset}__{timestamp}"

        # Run in background thread to avoid blocking
        def run_in_background():
            try:
                api.run(
                    method=method,
                    dataset=dataset,
                    build=build,
                    gpu=gpu,
                    method_params=params
                )
            except Exception as e:
                print(f"[ERROR] Background run error: {e}")

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
    try:
        print(f"[DEBUG] Starting evaluation for {experiment_name}")
        
        # Run evaluation in background to avoid blocking
        def run_evaluation():
            try:
                api.evaluate_experiment(experiment_name)
                print(f"[DEBUG] Evaluation completed for {experiment_name}")
            except Exception as e:
                print(f"[ERROR] Evaluation error for {experiment_name}: {e}")
        
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
    try:
        print(f"[DEBUG] Stopping experiment {experiment_name}")
        
        # Run stop in background to avoid blocking
        def stop_in_background():
            try:
                success = api.stop_experiment(experiment_name)
                print(f"[DEBUG] Stop result for {experiment_name}: {success}")
            except Exception as e:
                print(f"[ERROR] Stop error for {experiment_name}: {e}")
        
        thread = threading.Thread(target=stop_in_background, daemon=True)
        thread.start()
        
        return jsonify({'success': True, 'message': 'Stop request sent'})
    except Exception as e:
        print(f"[ERROR] Unexpected error in stop: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/experiments/<experiment_name>/delete', methods=['POST'])
def delete_experiment(experiment_name):
    """Delete an experiment (stop service + remove directory)."""
    try:
        print(f"[DEBUG] Deleting experiment {experiment_name}")

        def delete_in_background():
            try:
                success = api.delete_experiment(experiment_name)
                print(f"[DEBUG] Delete result for {experiment_name}: {success}")
            except Exception as e:
                print(f"[ERROR] Delete error for {experiment_name}: {e}")

        thread = threading.Thread(target=delete_in_background, daemon=True)
        thread.start()

        return jsonify({'success': True, 'message': 'Delete request sent'})
    except Exception as e:
        print(f"[ERROR] Unexpected error in delete: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/experiments/<experiment_name>/logs')
def experiment_logs(experiment_name):
    """Get experiment logs."""
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
        """Fetch experiments and broadcast if changed."""
        try:
            experiments = api.list_experiments(limit=50)
            experiments_data = [e.to_dict() for e in experiments]
            
            with state_lock:
                # Only broadcast if changed
                if experiments_data != last_state['experiments']:
                    broadcast_update('experiments', experiments_data)
                    last_state['experiments'] = experiments_data
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
            
            # Fetch experiments and services in parallel threads
            exp_thread = threading.Thread(target=fetch_and_broadcast_experiments, daemon=True)
            svc_thread = threading.Thread(target=fetch_and_broadcast_services, daemon=True)
            
            exp_thread.start()
            svc_thread.start()
            
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
