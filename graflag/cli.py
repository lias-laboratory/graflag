"""Command Line Interface for GraFlag."""

import os
import sys
import json
import argparse
import logging
import traceback
from pathlib import Path

from .core import GraFlag, GraFlagError
from .config import get_config_path, init_config

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def main():
    """Main CLI interface."""
    parser = argparse.ArgumentParser(
        description="GraFlag - Graph Anomaly Detection Benchmarking Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  graflag setup                                    # Setup cluster
  graflag setup --reconfigure                      # Re-run config wizard
  graflag run --method Dummy --dataset Cora        # Run experiment with GPU
  graflag run -m taddy -d uci --build --params MAX_EPOCH=100 LEARNING_RATE=0.001
  graflag run -m DeepWalk -d CiteSeer --no-gpu     # Run without GPU
  graflag run --from-config ./experiments/exp__method__dataset__timestamp/service_config.json
  graflag status                                   # Show cluster status
  graflag list methods                             # List available methods
  graflag list services                            # List running services
  graflag logs -e exp__dummy__cora__20250924_161245 # Show logs
  graflag logs -e exp__dummy__cora__20250924_161245 -f # Follow logs
  graflag stop -e exp__dummy__cora__20250924_161245 # Stop experiment
  graflag evaluate -e exp__generaldyg__btc_alpha__20251211_120000 # Evaluate
  graflag copy -s ./data -d datasets -r            # Copy to remote
  graflag copy --from-remote -s experiments/exp -d ./local # Copy from remote
  graflag sync                                     # Sync current method dir
  graflag sync --lib --path ./my-lib/              # Sync a shared library
  graflag gui                                      # Start web dashboard
  graflag gui --port 8080 --debug                  # GUI on custom port
  graflag devcluster --hosts hosts.yml             # Deploy virtual cluster
  graflag devcluster --hosts hosts.yml --pubkey ~/.ssh/id_rsa.pub
  graflag devcluster --down                        # Stop and remove cluster
        """,
    )

    parser.add_argument(
        "command",
        choices=["setup", "run", "status", "list", "copy", "logs", "stop", "evaluate", "sync", "gui", "devcluster"],
        help="Command to execute",
    )
    parser.add_argument(
        "subcommand", nargs="?",
        choices=["methods", "datasets", "experiments", "services"],
        help="Subcommand for list command",
    )
    parser.add_argument("--method", "-m", help="Method name for run")
    parser.add_argument("--dataset", "-d", help="Dataset name for run")
    parser.add_argument("--tag", "-t", default="latest", help="Docker image tag (default: latest)")
    parser.add_argument("--build", "-b", action="store_true", help="Build image before running")
    parser.add_argument("--config", "-c", default=".env", help="Configuration file (default: .env)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    parser.add_argument("--source", "-s", nargs='+', help="Source path(s) for copy command")
    parser.add_argument("--dest", help="Destination path for copy command")
    parser.add_argument("--recursive", "-r", action="store_true", help="Copy directories recursively")
    parser.add_argument("--from-remote", action="store_true", help="Copy from remote to local")
    parser.add_argument("--experiment", "-e", help="Experiment name for logs/stop commands")
    parser.add_argument("--follow", "-f", action="store_true", help="Follow log output")
    parser.add_argument("--rm", action="store_true", help="Also delete experiment directory on stop")
    parser.add_argument("--tee", help="Save logs to file while displaying")
    parser.add_argument("--gpu", "-g", action="store_true", default=True, help="Enable GPU (default: True)")
    parser.add_argument("--no-gpu", action="store_false", dest="gpu", help="Disable GPU")
    parser.add_argument("--params", "-p", nargs='+', metavar="KEY=VALUE", help="Method parameters")
    parser.add_argument(
        "--from-config", metavar="CONFIG_FILE",
        help="Load method/dataset/params from a config file",
    )
    parser.add_argument("--lib", action="store_true", help="Sync as a shared library")
    parser.add_argument("--path", default=None, help="Local path for sync command")
    # GUI args
    parser.add_argument("--host", default="0.0.0.0", help="GUI server host (default: 0.0.0.0)")
    parser.add_argument("--port", default=5000, type=int, help="GUI server port (default: 5000)")
    parser.add_argument("--debug", action="store_true", help="Enable GUI debug mode")
    # Devcluster args
    parser.add_argument("--hosts", default=None, help="Path to hosts.yml for devcluster")
    parser.add_argument("--pubkey", default=None, help="Path to SSH public key for devcluster")
    parser.add_argument("--down", action="store_true", help="Stop and remove devcluster")
    parser.add_argument("--reconfigure", action="store_true", help="Re-run configuration wizard for setup")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        # GUI: start web dashboard (doesn't need GraFlag instance directly)
        if args.command == "gui":
            from .gui.server import serve
            serve(args.config, args.host, args.port, args.debug)
            return

        # Devcluster: deploy or tear down virtual cluster
        if args.command == "devcluster":
            if not args.hosts and not args.down:
                parser.error("devcluster requires --hosts <path-to-hosts.yml> or --down")
            from .devcluster.cli import main as devcluster_main
            devcluster_main(args.hosts, args.pubkey, args.down)
            return

        # Setup: create or update config interactively
        if args.command == "setup":
            config_path = get_config_path(args.config)
            if not config_path.exists() or args.reconfigure:
                init_config()

        gf = GraFlag(config_file=args.config)

        if args.command == "setup":
            gf.setup()
            # Show status after setup
            _print_status(gf.status())

        elif args.command == "run":
            method, dataset, method_params = _parse_run_args(args, parser)
            gf.run(method, dataset, args.tag, args.build, args.gpu, method_params)

        elif args.command == "status":
            _print_status(gf.status())

        elif args.command == "list":
            if args.subcommand == "methods":
                _print_methods(gf.list_methods())
            elif args.subcommand == "datasets":
                _print_datasets(gf.list_datasets())
            elif args.subcommand == "experiments":
                _print_experiments(gf.list_experiments())
            elif args.subcommand == "services":
                _print_services(gf.list_services())
            else:
                parser.error("list command requires subcommand: methods, datasets, experiments, or services")

        elif args.command == "copy":
            if not args.source or not args.dest:
                parser.error("copy command requires --source and --dest")
            gf.copy_files(args.source, args.dest, args.recursive, args.from_remote)

        elif args.command == "logs":
            if not args.experiment:
                parser.error("logs command requires --experiment")
            if args.follow:
                gf.follow_logs(args.experiment, args.tee)
            else:
                gf.show_logs(args.experiment, args.tee)

        elif args.command == "stop":
            if not args.experiment:
                parser.error("stop command requires --experiment")
            gf.stop(args.experiment, remove=args.rm)

        elif args.command == "evaluate":
            if not args.experiment:
                parser.error("evaluate command requires --experiment")
            gf.evaluate(args.experiment)

        elif args.command == "sync":
            local_path = args.path or os.getcwd()
            gf.sync(local_path, is_lib=args.lib)

    except GraFlagError as e:
        logger.error(str(e))
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        if args.verbose:
            traceback.print_exc()
        sys.exit(1)


# ============================================================================
# Output Formatting
# ============================================================================

def _parse_run_args(args, parser):
    """Parse run arguments from CLI args."""
    method = args.method
    dataset = args.dataset
    method_params = {}

    if args.from_config:
        config_path = Path(args.from_config)
        if not config_path.exists():
            parser.error(f"Config file not found: {args.from_config}")

        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
        except json.JSONDecodeError as e:
            parser.error(f"Invalid JSON in config file: {e}")

        if not method:
            method = config.get('method_name')
        if not dataset:
            dataset = config.get('dataset')

        env_contents = config.get('env_contents', {})
        for key, value in env_contents.items():
            if key.startswith('_'):
                method_params[key[1:]] = str(value)

    if args.params:
        for param in args.params:
            if '=' not in param:
                parser.error(f"Invalid parameter format: {param}. Use KEY=VALUE.")
            key, value = param.split('=', 1)
            method_params[key] = value

    if not method or not dataset:
        parser.error("run requires --method and --dataset (or --from-config)")

    return method, dataset, method_params


def _print_status(cluster_info):
    """Format and print cluster status."""
    if cluster_info.error:
        print(f"[ERROR] {cluster_info.error}")
        return

    print(f"\n[INFO] Manager: {cluster_info.manager_ip}")
    print(f"[INFO] Swarm: {'active' if cluster_info.swarm_initialized else 'inactive'}")

    if cluster_info.worker_nodes:
        print("\n[INFO] Nodes:")
        for node in cluster_info.worker_nodes:
            role = "manager" if node.get('is_manager') else "worker"
            print(f"  - {node['hostname']}: {node['status']} ({role}, {node['availability']})")

    if cluster_info.services:
        print(f"\n[INFO] Running Services:")
        print(f"  {'NAME':<50} {'REPLICAS':<15} {'IMAGE':<30}")
        print("  " + "-" * 95)
        for svc in cluster_info.services:
            name = svc['name'][:49]
            replicas = svc.get('replicas', '')[:14]
            image = svc.get('image', '')[:29]
            print(f"  {name:<50} {replicas:<15} {image:<30}")
    else:
        print("\n[INFO] Running Services: None")

    print(f"\n[INFO] Shared Directory: {cluster_info.shared_dir}")
    if cluster_info.shared_contents:
        print("  Contents:")
        for item in cluster_info.shared_contents:
            print(f"    - {item}")


def _print_methods(methods):
    """Format and print method list."""
    if not methods:
        print("[INFO] No methods found")
        return

    print("[INFO] Available Methods:")
    for m in methods:
        print(f"  - {m.name} (Supports: {m.supported_data})")


def _print_datasets(datasets):
    """Format and print dataset list."""
    if not datasets:
        print("[INFO] No datasets found")
        return

    print("[INFO] Available Datasets:")
    for d in datasets:
        size_str = f" ({d.size_mb:.1f} MB, {d.file_count} files)" if d.size_mb > 0 else ""
        print(f"  - {d.name}{size_str}")


def _print_experiments(experiments):
    """Format and print experiment list."""
    if not experiments:
        print("[INFO] No experiments found")
        return

    print("[INFO] Recent Experiments:")
    for e in experiments:
        tags = f"[{e.status}]"
        if e.has_results:
            tags += " [results]"
        if e.has_evaluation:
            tags += " [eval]"
        print(f"  - {e.name}  {tags}")


def _print_services(services):
    """Format and print running services."""
    if not services:
        print("\n[INFO] Running Services: None")
        return

    print("\n[INFO] Running Services:")
    print(f"  {'NAME':<50} {'REPLICAS':<15} {'IMAGE':<30}")
    print("  " + "-" * 95)
    for svc in services:
        name = svc['name'][:49]
        replicas = str(svc.get('replicas', ''))[:14]
        image = svc.get('image', '')[:29]
        print(f"  {name:<50} {replicas:<15} {image:<30}")


if __name__ == "__main__":
    main()
